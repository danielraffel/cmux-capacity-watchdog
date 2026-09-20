#!/usr/bin/env python3
"""Watch cmux agent sessions and resume ones that stopped on provider-capacity errors.

State-aware: a session whose goal paused gets "/goal resume"; a session with no
goal that stopped on a capacity-class error gets "continue". Sessions that are
busy, that stopped cleanly, or whose composer has unsent text are never touched.

Usage:
    cmux-capacity-watchdog.py --surface <id|ref> [--surface ...] [--interval 20] [--dry-run] [--log FILE]
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"

# A turn is running when any of these appear in the tail.
BUSY_MARKERS = ("esc to interrupt",)
# The goal is paused (Codex/Kimi footer wording).
GOAL_PAUSED_MARKERS = ("goal paused",)
# The turn died on a transient provider problem worth retrying. User interrupts
# (Esc) and clean completions never match these.
CAPACITY_MARKERS = (
    "selected model is at capacity",
    "model is at capacity",
    "server_is_overloaded",
    "servers are currently overloaded",
    "stream disconnected before completion",
    "stream error",
    "error: 503",
    "503 service unavailable",
    "connection error",
)
# Known empty-composer placeholders by client.
COMPOSER_PLACEHOLDERS = ("ask codex to do anything", "ask kimi", "type a message", "ask anything")

# A surface counts as a Codex session when its composer placeholder or model
# slug is visible. Works on busy turns too — the placeholder stays on screen.
CODEX_SESSION_MARKERS = ("ask codex to do anything", "gpt-")
DISCOVERY_INTERVAL = 300.0  # seconds between scans for new Codex sessions

MAX_ACTIONS_PER_STOP = 3
ACTION_BACKOFF = (30, 60, 120)  # seconds between repeated actions on the same stop
STALL_POLLS_REQUIRED = 2        # an idle "pursuing goal" footer must persist before acting
SLOW_LANE_INTERVAL = 900        # after the fast attempts: one retry every 15 min until the storm passes
VERIFY_WAIT = 4.0


def cmux(*args: str) -> str:
    result = subprocess.run([CMUX, *args], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"cmux {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def read_tail(surface: str, lines: int = 30) -> str:
    return cmux("read-screen", "--surface", surface, "--lines", str(lines))


def send_literal(surface: str, text: str) -> None:
    """Send control input (slash command or plain prompt) at column zero.

    Per the tab-coordination rules: slash commands are client control input and
    must never go through an attributing peer-message path; Enter follows only
    after the paste-debounce window.
    """
    cmux("send", "--surface", surface, text)
    time.sleep(1.2)
    cmux("send-key", "--surface", surface, "enter")


def composer_has_text(tail: str) -> bool:
    for line in tail.splitlines():
        stripped = line.strip()
        if stripped.startswith("›"):
            content = stripped.lstrip("›").strip()
            if not content:
                continue
            if any(content.lower().startswith(p) for p in COMPOSER_PLACEHOLDERS):
                continue
            return True
    return False


def classify(tail: str) -> tuple:
    """Return (state, matched_marker). The marker is recorded in stats so we
    can see which failure signatures dominate and which we miss."""
    lowered = tail.lower()
    if any(marker in lowered for marker in BUSY_MARKERS):
        return "busy", ""
    for marker in GOAL_PAUSED_MARKERS:
        if marker in lowered:
            return "goal-paused", marker
    for marker in CAPACITY_MARKERS:
        if marker in lowered:
            return "capacity-stopped", marker
    if "pursuing goal" in lowered:
        # Footer claims an active goal but no turn is running: a silent stall
        # the client never surfaced as an error. Needs persistence across
        # polls before acting (between-turn gaps are seconds).
        return "goal-stalled-candidate", "pursuing goal (idle)"
    return "idle", ""


def tail_excerpt(tail: str, limit: int = 300) -> str:
    """Last few non-empty lines, so a stop's actual error text is on record."""
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    excerpt = " | ".join(lines[-4:])
    return excerpt[:limit]


def fingerprint(state: str, tail: str) -> str:
    # Strip digits so ticking timers and rotating reset times do not make every
    # poll look like a new stop.
    normalized = "".join("#" if c.isdigit() else c for c in tail.lower())
    return hashlib.sha1(f"{state}\n{normalized}".encode()).hexdigest()[:12]


@dataclass
class Watcher:
    surface: str
    title: str = ""
    last_fingerprint: str = ""
    actions_on_stop: int = 0
    next_action_at: float = 0.0
    parked: bool = False
    stop_since: float = 0.0
    stalled_polls: int = 0


@dataclass
class Config:
    interval: float
    dry_run: bool
    log_file: str
    stats_file: str
    watchers: dict = field(default_factory=dict)  # surface -> Watcher


def log(cfg: Config, message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    if cfg.log_file:
        with open(cfg.log_file, "a") as handle:
            handle.write(line + "\n")


def record(cfg: Config, event: str, **fields) -> None:
    """Append one structured stats event as a JSON line; never blocks the loop."""
    if not cfg.stats_file:
        return
    payload = {"ts": datetime.now().isoformat(timespec="seconds"),
               "host": socket.gethostname(), "event": event, **fields}
    try:
        with open(cfg.stats_file, "a") as handle:
            handle.write(json.dumps(payload) + "\n")
    except Exception:
        pass


def notify(title: str, body: str) -> None:
    try:
        cmux("notify", "--title", title, "--body", body)
    except Exception:
        pass


def verify_resumed(surface: str) -> tuple:
    """Check a few times over ~10s whether a turn started; return (confirmed,
    post-state, post-tail) so failures keep their evidence."""
    state, tail = "", ""
    for attempt in range(3):
        time.sleep(VERIFY_WAIT if attempt == 0 else 3.0)
        tail = read_tail(surface)
        state, _ = classify(tail)
        if state == "busy":
            return True, state, tail
    return False, state, tail


def act(cfg: Config, watcher: Watcher, state: str, tail: str, marker: str = "") -> None:
    command = "continue" if state == "capacity-stopped" else "/goal resume"
    now = time.time()
    fp = fingerprint(state, tail)
    if fp != watcher.last_fingerprint:
        # A new stop (or the first one seen): reset the per-stop bookkeeping.
        watcher.last_fingerprint = fp
        watcher.actions_on_stop = 0
        watcher.next_action_at = 0.0
        watcher.parked = False
        watcher.stop_since = now
        record(cfg, "stop_detected", surface=watcher.surface, title=watcher.title,
               state=state, marker=marker, fingerprint=fp, excerpt=tail_excerpt(tail))
    if now < watcher.next_action_at:
        return
    if composer_has_text(tail):
        log(cfg, f"{watcher.surface}: {state} but composer has unsent text; leaving it alone")
        record(cfg, "composer_blocked", surface=watcher.surface, state=state)
        watcher.next_action_at = now + 60
        return
    if watcher.actions_on_stop >= MAX_ACTIONS_PER_STOP:
        # Fast attempts are spent; drop to the slow lane instead of locking up
        # or giving up — a capacity storm passes, and one gentle retry every
        # 15 minutes rides it out without hammering the client.
        if not watcher.parked:
            watcher.parked = True
            log(cfg, f"{watcher.surface}: slow lane after {watcher.actions_on_stop} quick attempts; retrying every {SLOW_LANE_INTERVAL // 60}m")
            record(cfg, "slow_lane", surface=watcher.surface, attempts=watcher.actions_on_stop)
            notify("watchdog: session in slow lane", f"{watcher.surface} keeps failing to resume")
        watcher.next_action_at = now + SLOW_LANE_INTERVAL
        return
    watcher.actions_on_stop += 1
    backoff = ACTION_BACKOFF[min(watcher.actions_on_stop - 1, len(ACTION_BACKOFF) - 1)]
    watcher.next_action_at = now + backoff
    if cfg.dry_run:
        log(cfg, f"{watcher.surface}: DRY-RUN would send {command!r} (attempt {watcher.actions_on_stop})")
        return
    lane = "fast" if watcher.actions_on_stop <= MAX_ACTIONS_PER_STOP else "slow"
    log(cfg, f"{watcher.surface}: {state}; sending {command!r} (attempt {watcher.actions_on_stop}, {lane} lane)")
    record(cfg, "action", surface=watcher.surface, title=watcher.title,
           state=state, marker=marker, command=command, attempt=watcher.actions_on_stop, lane=lane)
    try:
        send_literal(watcher.surface, command)
        confirmed, post_state, post_tail = verify_resumed(watcher.surface)
        if confirmed:
            elapsed = round(now - watcher.stop_since, 1) if watcher.stop_since else None
            log(cfg, f"{watcher.surface}: resumed, turn is running")
            record(cfg, "resumed", surface=watcher.surface, title=watcher.title,
                   command=command, attempt=watcher.actions_on_stop, lane=lane, since_stop_s=elapsed)
        else:
            # The post-attempt screen is the evidence for why a resume missed:
            # wrong state read, client rejected the command, dialog in the way.
            log(cfg, f"{watcher.surface}: sent {command!r} but no turn is running (now: {post_state}); will re-check")
            record(cfg, "resume_unconfirmed", surface=watcher.surface, title=watcher.title,
                   command=command, attempt=watcher.actions_on_stop, lane=lane,
                   post_state=post_state, post_excerpt=tail_excerpt(post_tail))
    except Exception as exc:
        log(cfg, f"{watcher.surface}: send failed: {exc}")
        record(cfg, "send_error", surface=watcher.surface, command=command, error=str(exc))


def discover_codex_surfaces(cfg: Config) -> None:
    """Find every Codex session known to cmux and add it to the watch set.

    Enumeration is workspace -> pane surfaces -> a short screen read; the
    composer placeholder/model slug identifies Codex regardless of the tab
    title. Discovered surfaces that later vanish are dropped on read errors.
    """
    import json
    import re

    try:
        raw = cmux("list-workspaces", "--json", "--id-format", "both")
        data = json.loads(raw)
        workspaces = data if isinstance(data, list) else data.get("workspaces", [])
    except Exception as exc:
        log(cfg, f"discovery: list-workspaces failed: {exc}")
        return
    surface_re = re.compile(r"surface:\d+\s+([0-9A-Fa-f-]{36})\s*(.*)")
    for workspace in workspaces:
        wid = workspace.get("id")
        if not wid:
            continue
        try:
            listing = cmux("list-pane-surfaces", "--workspace", wid, "--id-format", "both")
        except Exception:
            continue
        for sid, title in surface_re.findall(listing):
            if sid in cfg.watchers:
                continue
            try:
                tail = read_tail(sid, lines=8)
            except Exception:
                continue
            lowered = tail.lower()
            if any(marker in lowered for marker in CODEX_SESSION_MARKERS):
                cfg.watchers[sid] = Watcher(surface=sid, title=title.strip())
                log(cfg, f"discovery: now watching Codex session {sid} ({title.strip()})")


def report(stats_file: str) -> int:
    """Aggregate the JSONL stats into the numbers that matter for tuning."""
    events = []
    try:
        with open(stats_file) as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError as exc:
        print(f"no stats yet ({exc})")
        return 1
    if not events:
        print("no stats yet")
        return 1

    stops = [e for e in events if e.get("event") == "stop_detected"]
    actions = [e for e in events if e.get("event") == "action"]
    resumed = [e for e in events if e.get("event") == "resumed"]
    unconfirmed = [e for e in events if e.get("event") == "resume_unconfirmed"]
    send_errors = [e for e in events if e.get("event") == "send_error"]
    blocked = [e for e in events if e.get("event") == "composer_blocked"]
    slow = [e for e in events if e.get("event") == "slow_lane"]

    print(f"events: {len(events)}  (since {events[0].get('ts', '?')})")
    print(f"stops detected: {len(stops)}")
    by_state = {}
    for e in stops:
        by_state[e.get("state", "?")] = by_state.get(e.get("state", "?"), 0) + 1
    for state, count in sorted(by_state.items()):
        print(f"  {state}: {count}")
    print(f"resume actions: {len(actions)}  "
          f"(goal resume: {sum(1 for e in actions if e.get('command') == '/goal resume')}, "
          f"continue: {sum(1 for e in actions if e.get('command') == 'continue')})")
    print(f"resumed OK: {len(resumed)}  unconfirmed: {len(unconfirmed)}  send errors: {len(send_errors)}")
    if resumed:
        attempts = sorted(e.get("attempt", 0) for e in resumed)
        latencies = sorted(e.get("since_stop_s") for e in resumed if e.get("since_stop_s") is not None)
        print(f"attempts to resume: median {attempts[len(attempts) // 2]}, max {attempts[-1]}")
        if latencies:
            mid = len(latencies) // 2
            median = latencies[mid] if len(latencies) % 2 else (latencies[mid - 1] + latencies[mid]) / 2
            print(f"time-to-resume: median {median}s, max {latencies[-1]}s")
    print(f"composer-blocked deferrals: {len(blocked)}  slow-lane entries: {len(slow)}")

    # Which error signatures are killing sessions, with one example each —
    # this is how we decide what to add to or tune in the marker list.
    by_marker = {}
    for e in stops:
        by_marker.setdefault(e.get("marker") or "(unknown)", []).append(e)
    if by_marker:
        print("stops by matched marker:")
        for marker, marker_events in sorted(by_marker.items(), key=lambda kv: -len(kv[1])):
            print(f"  {marker}: {len(marker_events)}")
            example = marker_events[-1].get("excerpt")
            if example:
                print(f"    e.g. {example[:180]}")

    # Fast vs slow lane: how much work each lane does and whether the slow
    # lane ever lands a resume — the input for tuning the 15-minute cool-off.
    fast_actions = [e for e in actions if e.get("lane") == "fast"]
    slow_actions = [e for e in actions if e.get("lane") == "slow"]
    if fast_actions or slow_actions:
        slow_resumed = sum(1 for e in resumed if e.get("lane") == "slow")
        print(f"lanes: fast {len(fast_actions)} actions, slow {len(slow_actions)} actions "
              f"({slow_resumed} slow-lane resumes confirmed)")

    # Flap: a fresh stop on the same surface within 5 minutes of a confirmed
    # resume — the "they fall over again" rate.
    flaps = 0
    by_surface = {}
    for e in events:
        by_surface.setdefault(e.get("surface", "?"), []).append(e)
    for surface_events in by_surface.values():
        last_resume = None
        for e in surface_events:
            try:
                ts = datetime.fromisoformat(e.get("ts", "")).timestamp()
            except ValueError:
                continue
            if e.get("event") == "resumed":
                last_resume = ts
            elif e.get("event") == "stop_detected" and last_resume and ts - last_resume < 300:
                flaps += 1
    if resumed:
        print(f"flap rate: {flaps}/{len(resumed)} resumes saw a new stop within 5m")

    per_surface = {}
    for e in stops:
        per_surface[e.get("surface", "?")] = per_surface.get(e.get("surface", "?"), 0) + 1
    if per_surface:
        print("stops by surface (top 10):")
        for surface, count in sorted(per_surface.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {surface}: {count}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--surface", action="append", default=[], help="cmux surface id or ref; repeatable")
    parser.add_argument("--all-codex", action="store_true", help="also discover every Codex session cmux knows about")
    parser.add_argument("--interval", type=float, default=20.0)
    parser.add_argument("--dry-run", action="store_true", help="classify and log, never send")
    parser.add_argument("--log", default="", help="append actions to this file")
    parser.add_argument("--stats", default=os.path.expanduser("~/.local/state/cmux-capacity-watchdog.jsonl"),
                        help="append structured stats events to this JSONL file")
    parser.add_argument("--report", action="store_true", help="print aggregate stats and exit")
    args = parser.parse_args()
    if args.report:
        return report(args.stats)
    if not args.surface and not args.all_codex:
        parser.error("give at least one --surface or pass --all-codex")

    cfg = Config(interval=args.interval, dry_run=args.dry_run, log_file=args.log, stats_file=args.stats)
    for surface in args.surface:
        cfg.watchers[surface] = Watcher(surface=surface)
    log(cfg, f"watching {len(cfg.watchers)} surface(s), interval {cfg.interval}s, dry_run={cfg.dry_run}, all_codex={args.all_codex}")

    last_discovery = 0.0
    while True:
        if args.all_codex and time.time() - last_discovery >= DISCOVERY_INTERVAL:
            discover_codex_surfaces(cfg)
            last_discovery = time.time()
        for surface, watcher in list(cfg.watchers.items()):
            try:
                tail = read_tail(watcher.surface)
                state, marker = classify(tail)
                if state == "busy":
                    # A running turn proves the last resume worked; re-arm.
                    watcher.last_fingerprint = ""
                    watcher.actions_on_stop = 0
                    watcher.parked = False
                    watcher.stalled_polls = 0
                    continue
                if state == "goal-stalled-candidate":
                    # Between-turn gaps are seconds; only a persistent idle
                    # "pursuing goal" footer is a real stall.
                    watcher.stalled_polls += 1
                    if watcher.stalled_polls < STALL_POLLS_REQUIRED:
                        continue
                    state = "goal-stalled"
                else:
                    watcher.stalled_polls = 0
                if state in ("goal-paused", "capacity-stopped", "goal-stalled"):
                    act(cfg, watcher, state, tail, marker)
            except Exception as exc:
                if surface in args.surface:
                    log(cfg, f"{watcher.surface}: poll error: {exc}")
                else:
                    # Discovered surface went away (tab closed); drop it.
                    log(cfg, f"discovery: stopped watching {surface} ({exc})")
                    del cfg.watchers[surface]
        time.sleep(cfg.interval)


if __name__ == "__main__":
    sys.exit(main())
