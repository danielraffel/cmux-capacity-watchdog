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
                              lambda surface, text, *_: self.sends.append((surface, text))),
            mock.patch.object(watchdog, "verify_resumed", lambda surface, *_: (True, "busy", "")),
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
                               lambda surface, *_: (False, "goal-paused", STOPPED_TAIL)):
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
                               lambda surface, *_: (False, "goal-paused", STOPPED_TAIL)):
            watchdog.poll_surface(cfg, watcher, pinned=set())
        self.assertTrue(watcher.parked)
        self.assertAlmostEqual(watcher.next_action_at - time.time(),
                               watchdog.SLOW_LANE_INTERVAL, delta=2.0)

    def test_stop_detected_records_detection_lag(self):
        """stop_detected carries since_busy_s so detection lag is measurable."""
        events = []
        watcher = make_watcher("F")
        cfg = make_cfg()
        cfg.watchers = {"F": watcher}
        with mock.patch.object(watchdog, "read_tail", return_value=STOPPED_TAIL), \
             mock.patch.object(watchdog, "record",
                               lambda cfg, event, **fields: events.append({"event": event, **fields})):
            watchdog.poll_surface(cfg, watcher, pinned=set())
        detected = [e for e in events if e["event"] == "stop_detected"]
        self.assertEqual(len(detected), 1)
        self.assertAlmostEqual(detected[0]["since_busy_s"], 0.0, delta=2.0)

    def test_next_wake_earliest_due(self):
        now = time.time()
        cfg = make_cfg()
        cfg.watchers = {"A": make_watcher("A", next_action_at=now + 30),
                        "B": make_watcher("B", next_action_at=now + 3600)}
        self.assertAlmostEqual(watchdog.next_wake(cfg), 30, delta=2.0)
        cfg.watchers["A"].next_action_at = 0.0
        cfg.watchers["B"].next_action_at = 0.0
        self.assertEqual(watchdog.next_wake(cfg), cfg.interval)


FIXTURES = os.path.join(_HERE, "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as handle:
        return handle.read()


class ClaudeScreenTest(unittest.TestCase):
    """Real Claude Code 2.1.307 screens, captured against e2e/anthropic_stub.py."""

    def classify(self, name):
        tail = fixture(name)
        self.assertEqual(watchdog.detect_agent(tail, watchdog.PROFILES), "claude", name)
        return watchdog.classify(tail, watchdog.PROFILES["claude"])

    def test_final_503_is_resume_evidence(self):
        state, marker = self.classify("claude-503-final-error.txt")
        self.assertEqual(state, "capacity-stopped")
        self.assertTrue(marker)

    def test_own_retry_loop_is_retrying_not_busy(self):
        self.assertEqual(self.classify("claude-503-retrying-busy.txt")[0], "retrying")

    def test_spinner_is_busy(self):
        self.assertEqual(self.classify("claude-continue-busy.txt")[0], "busy")
        self.assertEqual(self.classify("claude-busy-spinner-queued.txt")[0], "busy")

    def test_old_error_then_normal_reply_is_not_evidence(self):
        self.assertEqual(self.classify("claude-finished-after-old-error.txt")[0], "idle")

    def test_interrupt_restores_draft_and_blocks_action(self):
        tail = fixture("claude-user-interrupted.txt")
        profile = watchdog.PROFILES["claude"]
        self.assertEqual(watchdog.classify(tail, profile)[0], "idle")
        self.assertTrue(watchdog.composer_has_text(tail, profile))

    def test_user_prompt_after_error_is_not_evidence(self):
        tail = fixture("claude-503-final-error.txt").replace(
            "✻ Baked for", "❯ a new question\n\n✻ Baked for")
        self.assertEqual(watchdog.classify(tail, watchdog.PROFILES["claude"])[0], "idle")

    def test_selection_dialog_is_not_a_composer(self):
        tail = fixture("claude-trust-dialog.txt")
        self.assertEqual(watchdog.composer_content(tail, watchdog.PROFILES["claude"]), "")
        self.assertEqual(watchdog.detect_agent(tail, watchdog.PROFILES), "")

    def test_codex_screen_is_not_claude(self):
        self.assertEqual(watchdog.detect_agent(STOPPED_TAIL, watchdog.PROFILES), "codex")


class ClaudeRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.sends = []
        self._patches = [
            mock.patch.object(watchdog, "send_literal",
                              lambda surface, text, *_: self.sends.append((surface, text))),
            mock.patch.object(watchdog, "notify", lambda *a, **k: None),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()

    def test_sends_continue_and_uses_claude_ladder(self):
        watcher = make_watcher("K", agent="claude")
        cfg = make_cfg()
        cfg.watchers = {"K": watcher}
        error = fixture("claude-503-final-error.txt")
        retrying = fixture("claude-503-retrying-busy.txt")
        with mock.patch.object(watchdog, "read_tail", return_value=error), \
             mock.patch.object(watchdog, "verify_resumed", lambda surface, *_: (False, "retrying", retrying)):
            for attempt in range(watchdog.PROFILES["claude"].fast_attempts):
                if attempt:
                    watcher.next_action_at = time.time() - 1
                watchdog.poll_surface(cfg, watcher, pinned=set())
        self.assertEqual(self.sends, [("K", "continue")] * 3)
        self.assertTrue(watcher.parked)
        self.assertAlmostEqual(watcher.next_action_at - time.time(), 900, delta=2.0)

    def test_retry_after_our_send_is_not_a_witness(self):
        watcher = make_watcher("L", agent="claude", last_seen_busy=1.0, actions_on_stop=1,
                               pending={"command": "continue", "attempt": 1, "lane": "fast",
                                        "sent_at": time.time(), "stop_since": time.time()})
        cfg = make_cfg()
        cfg.watchers = {"L": watcher}
        with mock.patch.object(watchdog, "read_tail", return_value=fixture("claude-503-retrying-busy.txt")):
            watchdog.poll_surface(cfg, watcher, pinned=set())
        self.assertEqual(watcher.last_seen_busy, 1.0)
        self.assertTrue(watcher.pending)

    def test_short_resumed_turn_between_polls_counts_as_resumed(self):
        events = []
        watcher = make_watcher("N", agent="claude", actions_on_stop=1,
                               pending={"command": "continue", "attempt": 1, "lane": "fast",
                                        "sent_at": time.time() - 40, "stop_since": time.time() - 60})
        cfg = make_cfg()
        cfg.watchers = {"N": watcher}
        with mock.patch.object(watchdog, "read_tail", return_value=fixture("claude-finished-after-old-error.txt")), \
             mock.patch.object(watchdog, "record",
                               lambda cfg, event, **fields: events.append(event)):
            watchdog.poll_surface(cfg, watcher, pinned=set())
        self.assertIn("resumed", events)
        self.assertIsNone(watcher.pending)
        self.assertEqual(watcher.actions_on_stop, 0)

    def test_user_prompt_after_our_send_is_not_credited(self):
        events = []
        tail = fixture("claude-finished-after-old-error.txt").replace(
            "✻ Cogitated", "❯ something the user typed\n\n✻ Cogitated")
        watcher = make_watcher("O", agent="claude", actions_on_stop=1,
                               pending={"command": "continue", "attempt": 1, "lane": "fast",
                                        "sent_at": time.time() - 40, "stop_since": time.time() - 60})
        cfg = make_cfg()
        cfg.watchers = {"O": watcher}
        with mock.patch.object(watchdog, "read_tail", return_value=tail), \
             mock.patch.object(watchdog, "record",
                               lambda cfg, event, **fields: events.append(event)):
            watchdog.poll_surface(cfg, watcher, pinned=set())
        self.assertNotIn("resumed", events)
        self.assertIn("pending_cleared_idle", events)

    def test_retry_from_users_own_turn_is_a_witness(self):
        watcher = make_watcher("M", agent="claude", last_seen_busy=0.0)
        cfg = make_cfg()
        cfg.watchers = {"M": watcher}
        with mock.patch.object(watchdog, "read_tail", return_value=fixture("claude-503-retrying-busy.txt")):
            watchdog.poll_surface(cfg, watcher, pinned=set())
        self.assertGreater(watcher.last_seen_busy, time.time() - 5)


class RulesFileTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "rules.json")

    def write(self, text):
        with open(self.path, "w") as handle:
            handle.write(text)
        # Distinct mtimes so the loader sees each edit.
        stamp = time.time() + len(os.listdir(self.dir)) + getattr(self, "_n", 0)
        self._n = getattr(self, "_n", 0) + 1
        os.utime(self.path, (stamp, stamp))

    def test_add_replace_disable_and_bad_edit_keeps_last_good(self):
        loader = watchdog.RulesLoader(self.path)
        logs = []
        self.assertEqual(loader.load(logs.append)["codex"].slow_lane_interval, 300)
        self.write('{"agents": {"codex": {"slow_lane_interval": 600, '
                   '"evidence_contains_add": ["upstream connect error"]}, '
                   '"claude": {"enabled": false}}}')
        profiles = loader.load(logs.append)
        self.assertEqual(profiles["codex"].slow_lane_interval, 600)
        self.assertIn("upstream connect error", profiles["codex"].evidence_contains)
        self.assertIn("selected model is at capacity", profiles["codex"].evidence_contains)
        self.assertFalse(profiles["claude"].enabled)
        self.write('{"agents": {"codex": {"evidence_regex": ["(unclosed"]}}}')
        profiles = loader.load(logs.append)
        self.assertEqual(profiles["codex"].slow_lane_interval, 600)
        self.assertTrue(any("invalid" in line for line in logs))
        # Defaults are never mutated by an override.
        self.assertNotIn("upstream connect error", watchdog.DEFAULT_PROFILES["codex"]["evidence_contains"])


if __name__ == "__main__":
    unittest.main()
