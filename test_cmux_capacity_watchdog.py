#!/usr/bin/env python3
"""Tests for cmux-capacity-watchdog. Run: python3 test_cmux_capacity_watchdog.py

The core guarantee under test: recovery is round-robin — a session in maximum
backoff cannot delay detection or recovery of another session — while the
retry/backoff, confirmation, novelty, and retry-limit semantics are unchanged.
"""

import importlib.util
import os
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "watchdog", os.path.join(_HERE, "cmux-capacity-watchdog.py"))
watchdog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watchdog)

MARKER = "selected model is at capacity"
STOPPED_TAIL = (
    "some earlier output\n"
    "⚠ Selected model is at capacity. Please try a different model.\n"
    "› Ask Codex to do anything\n"
    "gpt-5.6 medium · ~/Code/x · Goal stalled (/goal resume)\n"
)


def make_cfg(**overrides):
    cfg = watchdog.Config(interval=60.0, dry_run=False, log_file="", stats_file="")
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def make_watcher(surface, **overrides):
    watcher = watchdog.Watcher(surface=surface, primed=True, last_seen_busy=time.time())
    for key, value in overrides.items():
        setattr(watcher, key, value)
    return watcher


class RoundRobinTest(unittest.TestCase):
    def setUp(self):
        self.sends = []
        self._patches = [
            mock.patch.object(watchdog, "send_literal",
                              lambda surface, text: self.sends.append((surface, text))),
            mock.patch.object(watchdog, "verify_resumed", lambda surface: (True, "busy", "")),
            mock.patch.object(watchdog, "notify", lambda *a, **k: None),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()

    def test_deep_backoff_session_cannot_delay_another(self):
        """A session sitting in maximum backoff must neither be sent to nor
        delay a second session whose fresh stop is due for recovery."""
        now = time.time()
        episode_fp = watchdog.fingerprint("goal-paused", MARKER, STOPPED_TAIL)
        backed_off = make_watcher("A", next_action_at=now + 3600, actions_on_stop=99,
                                  parked=True, marker_seen="", last_fingerprint=episode_fp)
        fresh = make_watcher("B")
        cfg = make_cfg()
        cfg.watchers = {"A": backed_off, "B": fresh}

        with mock.patch.object(watchdog, "read_tail", return_value=STOPPED_TAIL):
            for watcher in list(cfg.watchers.values()):
                watchdog.poll_surface(cfg, watcher, pinned=set())

        self.assertEqual(self.sends, [("B", "/goal resume")])
        # B recovered in one visit despite A's hour-long backoff; and the loop
        # would wake on the steady-state cadence, not on A's timer.
        self.assertEqual(watchdog.next_wake(cfg), cfg.interval)

    def test_fast_lane_schedule_preserved_across_visits(self):
        """Per-visit attempts keep the old inline ladder's 5s→60s spacing,
        with the 45s grace floor after an unconfirmed send."""
        watcher = make_watcher("C")
        cfg = make_cfg()
        cfg.watchers = {"C": watcher}
        expected = [45, 45, 45, 45, 60]  # 5/10/20/40/60 raised to the 45s grace floor
        with mock.patch.object(watchdog, "read_tail", return_value=STOPPED_TAIL), \
             mock.patch.object(watchdog, "verify_resumed",
                               lambda surface: (False, "goal-paused", STOPPED_TAIL)):
            for want in expected:
                before = time.time()
                watchdog.poll_surface(cfg, watcher, pinned=set())
                got = watcher.next_action_at - before
                self.assertAlmostEqual(got, want, delta=2.0)
                # Simulate time passing: the schedule is authoritative, so a
                # due retry fires even inside the 300s pending window.
                watcher.next_action_at = time.time() - 1
        self.assertEqual(watcher.actions_on_stop, len(expected))
        self.assertEqual(len(self.sends), len(expected))

    def test_pending_window_suppresses_unscheduled_send(self):
        """Without a due scheduled retry, the 300s confirmation window still
        suppresses sends after an unconfirmed submission."""
        now = time.time()
        watcher = make_watcher("D", actions_on_stop=1, next_action_at=now + 250,
                               pending={"command": "/goal resume", "attempt": 1,
                                        "lane": "fast", "sent_at": now - 50,
                                        "stop_since": now - 60},
                               last_fingerprint=watchdog.fingerprint("goal-paused", MARKER, STOPPED_TAIL))
        cfg = make_cfg()
        cfg.watchers = {"D": watcher}
        with mock.patch.object(watchdog, "read_tail", return_value=STOPPED_TAIL):
            watchdog.poll_surface(cfg, watcher, pinned=set())
        self.assertEqual(self.sends, [])

    def test_slow_lane_after_twelve_attempts(self):
        """The twelfth unconfirmed attempt hands off to the 5-minute slow lane."""
        watcher = make_watcher("E", actions_on_stop=watchdog.MAX_ACTIONS_PER_STOP - 1,
                               last_fingerprint=watchdog.fingerprint("goal-paused", MARKER, STOPPED_TAIL))
        cfg = make_cfg()
        cfg.watchers = {"E": watcher}
        with mock.patch.object(watchdog, "read_tail", return_value=STOPPED_TAIL), \
             mock.patch.object(watchdog, "verify_resumed",
                               lambda surface: (False, "goal-paused", STOPPED_TAIL)):
            watchdog.poll_surface(cfg, watcher, pinned=set())
        self.assertTrue(watcher.parked)
        self.assertAlmostEqual(watcher.next_action_at - time.time(),
                               watchdog.SLOW_LANE_INTERVAL, delta=2.0)

    def test_next_wake_earliest_due(self):
        now = time.time()
        cfg = make_cfg()
        cfg.watchers = {"A": make_watcher("A", next_action_at=now + 30),
                        "B": make_watcher("B", next_action_at=now + 3600)}
        self.assertAlmostEqual(watchdog.next_wake(cfg), 30, delta=2.0)
        cfg.watchers["A"].next_action_at = 0.0
        cfg.watchers["B"].next_action_at = 0.0
        self.assertEqual(watchdog.next_wake(cfg), cfg.interval)


if __name__ == "__main__":
    unittest.main()
