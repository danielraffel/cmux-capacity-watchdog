#!/usr/bin/env python3
"""Watch cmux agent sessions and resume ones that stopped on provider-capacity errors.

State-aware: a session whose goal paused gets "/goal resume"; a session with no
goal that stopped on a capacity-class error gets "continue". Resume is
evidence-gated: every action requires a known transient-error signature on
screen (capacity/overload/stream failure). Sessions that stopped any other way
— user pause, user interrupt, clean finish, deliberate abandonment — are never
touched; nor are busy turns or sessions with an unsent draft in the composer.

Steady-state polls are cheap (every 5 minutes by default); once a stop is
detected, the fast retry ladder runs inline at full speed (5s→60s backoff),
then a 5-minute slow lane rides out the storm. Every ladder attempt re-reads
the screen and aborts on user activity or a state change.

Usage:
    cmux-capacity-watchdog.py --surface <id|ref> [--surface ...] [--interval 300] [--dry-run] [--log FILE]
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

# Bump on EVERY behavior change. Every stats event carries this version, so
# logs and reports stay interpretable across policy changes; the git history
# maps versions to commits.
WATCHDOG_VERSION = "2026-09-19.14"

# A turn is running when any of these appear in the tail.
BUSY_MARKERS = ("esc to interrupt",)
# The goal is paused. Codex words it "Goal stalled (/goal resume)"; Kimi and
# others say "goal paused".
GOAL_PAUSED_MARKERS = ("goal paused", "goal stalled")
# The turn died on a transient provider problem worth retrying. User interrupts
# (Esc) and clean completions never match these. Deliberately narrow: the Codex
# model-capacity family plus the observed stream disconnect. Grow this list
# from the excerpts captured in stats, never speculatively.
CAPACITY_MARKERS = (
    "selected model is at capacity",
    "model is at capacity",
    "server_is_overloaded",
    "servers are currently overloaded",
    "stream disconnected before completion",
)
# Known empty-composer placeholders by client.
COMPOSER_PLACEHOLDERS = ("ask codex to do anything", "ask kimi", "type a message", "ask anything")

# A surface counts as a Codex session when its composer placeholder or model
# slug is visible. Works on busy turns too — the placeholder stays on screen.
CODEX_SESSION_MARKERS = ("ask codex to do anything", "gpt-")
DISCOVERY_INTERVAL = 300.0  # seconds between scans for new Codex sessions

# A stop is only resume-eligible when the watchdog personally saw the surface
# busy recently — a transition it witnessed. A session discovered already
# stopped (typical for deliberately abandoned tabs whose tails still show an
# old capacity error) is skipped. Witnessed-busy times persist across restarts.
# 8 hours: a full night's sleep, so an overnight storm still finds the session
# resume-eligible in the morning.
WITNESS_FRESHNESS = 8 * 3600  # seconds since last seen busy
WITNESS_FILE = os.path.expanduser("~/.local/state/cmux-capacity-watchdog-witness.json")


def load_witnesses(log_fn=print) -> dict:
    try:
        with open(WITNESS_FILE) as handle:
            return {k: float(v) for k, v in json.load(handle).items()}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        # Failing closed (treating every stop as unwitnessed) is safe, but it
        # must be loud: otherwise a corrupted file silently disables resumes.
        log_fn(f"witness file unreadable ({exc}); stopped sessions fail closed until seen busy")
        return {}


def save_witnesses(watchers: dict) -> None:
    data = {surface: w.last_seen_busy for surface, w in watchers.items() if w.last_seen_busy > 0}
    try:
        os.makedirs(os.path.dirname(WITNESS_FILE), exist_ok=True)
        with open(WITNESS_FILE + ".tmp", "w") as handle:
            json.dump(data, handle)
        os.replace(WITNESS_FILE + ".tmp", WITNESS_FILE)
    except OSError:
        pass


# Sessions that must never be touched: one rule per line in the ignore file.
# A bare line matches a surface UUID; "title:<text>" matches a title substring
# (case-insensitive). Read on every cycle, so edits take effect immediately —
# deliberately stopped sessions stay stopped.
IGNORE_FILE = os.path.expanduser("~/.config/cmux-capacity-watchdog/ignore")


def load_ignore_rules() -> tuple:
    """Return (ids, title_parts, ok). An unreadable ignore file fails CLOSED:
    exclusions are a safety boundary, so until the file reads again nothing
    is acted on at all."""
    ids, title_parts = set(), []
    try:
        with open(IGNORE_FILE) as handle:
            for line in handle:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                if line.lower().startswith("title:"):
                    title_parts.append(line[len("title:"):].strip().lower())
                else:
                    ids.add(line)
    except FileNotFoundError:
        pass  # no exclusions configured is a normal state
    except OSError as exc:
        print(f"WARNING: ignore file unreadable ({exc}); failing closed, no resumes until it reads", flush=True)
        return ids, title_parts, False
    return ids, title_parts, True


def is_ignored(rules, surface: str, title: str) -> bool:
    ids, title_parts, ok = rules
    if not ok:
        return True
    if surface in ids:
        return True
    lowered = title.lower()
    return any(part in lowered for part in title_parts)

# A dozen fast attempts with exponential backoff (5s, 10s, 20s, ... capped at
# 60s), then the slow lane: one retry every 5 minutes. The witness freshness
# window bounds the slow lane to ~2h after the last busy sighting.
MAX_ACTIONS_PER_STOP = 12
FAST_BACKOFF_CAP = 60           # seconds
STALL_POLLS_REQUIRED = 2        # an idle "pursuing goal" footer must persist before acting
SLOW_LANE_INTERVAL = 300        # after the fast attempts: one retry every 5 min until the storm passes
CONFIRM_GRACE = 45.0            # never send twice into a slow-starting turn
VERIFY_WAIT = 4.0


def cmux(*args: str) -> str:
    result = subprocess.run([CMUX, *args], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"cmux {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def read_tail(surface: str, lines: int = 30) -> str:
    return cmux("read-screen", "--surface", surface, "--lines", str(lines))


def composer_content(tail: str) -> str:
    """The text currently in the composer (best-effort, last composer line)."""
    for line in reversed(tail.splitlines()):
        stripped = line.strip()
        if stripped.startswith("›"):
            return stripped.lstrip("›").strip()
    return ""


class ComposerContaminated(Exception):
    """The composer changed around our send; aborted before Enter."""


class SubmitAmbiguous(Exception):
    """Enter may have landed before the failure — never auto-retry this stop."""


def send_literal(surface: str, text: str) -> None:
    """Send control input (slash command or plain prompt) at column zero.

    Per the tab-coordination rules: slash commands are client control input and
    must never go through an attributing peer-message path; Enter follows only
    after the paste-debounce window — and only when the composer still holds
    exactly our text. If the user started typing in the gap, our text is
    backed out when it is a clean suffix, and the send aborts either way.
    """
    cmux("send", "--surface", surface, text)
    time.sleep(1.2)
    content = composer_content(read_tail(surface))
    if content == text:
        # Residual race: one subprocess call (~100ms) sits between this read
        # and Enter. cmux has no atomic validate-and-submit; the repair path
        # below covers the larger pre-read window, and a lost 100ms race at
        # worst submits a mangled command the client rejects. Accepted.
        try:
            cmux("send-key", "--surface", surface, "enter")
        except Exception as exc:
            # A failure here is ambiguous: Enter may have landed before the
            # error. Never auto-retry this stop, or a later poll could
            # submit a duplicate command.
            raise SubmitAmbiguous(f"enter submission uncertain: {exc}") from exc
        return
    if content.endswith(text) and len(content) > len(text):
        # Our text landed after the user's fresh keystrokes; remove exactly
        # our suffix and leave their draft intact.
        for _ in range(len(text)):
            cmux("send-key", "--surface", surface, "backspace")
    raise ComposerContaminated(f"composer changed around our send ({content[:40]!r}); aborted without Enter")


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


# The current stop's error always sits just above the composer; old errors
# scroll out of the viewport. Matching against the whole tail would let stale
# scrollback qualify as evidence for a stop that happened hours later (e.g. a
# deliberate pause long after a capacity error), so only the last lines count.
EVIDENCE_LINES = 12


def classify(tail: str) -> tuple:
    """Return (state, matched_marker). The marker is recorded in stats so we
    can see which failure signatures dominate and which we miss."""
    lowered = "\n".join(tail.splitlines()[-EVIDENCE_LINES:]).lower()
    if any(marker in lowered for marker in BUSY_MARKERS):
        return "busy", ""
    for marker in GOAL_PAUSED_MARKERS:
        if marker in lowered:
            # A paused goal is only a candidate when a transient error is
            # visible too — users pause goals deliberately all the time, and
            # a deliberate pause has no error text on screen.
            for cap in CAPACITY_MARKERS:
                if cap in lowered:
                    return "goal-paused", cap
            return "goal-paused-no-error", marker
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


def fingerprint(state: str, marker: str, tail: str) -> str:
    # Identify the stop by its state, its evidence marker, and the last couple
    # of screen lines — not the whole tail, whose unrelated churn would
    # otherwise reset the per-stop attempt budget. Digits are stripped so
    # ticking timers and rotating reset times read as one stop.
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    relevant = " | ".join(lines[-2:]).lower()
    normalized = "".join("#" if c.isdigit() else c for c in relevant)
    return hashlib.sha1(f"{state}\n{marker}\n{normalized}".encode()).hexdigest()[:12]


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
    last_seen_busy: float = 0.0
    pending: object = None  # send awaiting late confirmation: {command, attempt, lane, sent_at, stop_since}
    primed: bool = False    # first observation only records marker state, never acts
    marker_seen: str = ""   # the error marker instance currently on screen
    skip_logged: bool = False  # stale/deliberate skips log once per stop episode


@dataclass
class Config:
    interval: float
    dry_run: bool
    log_file: str
    stats_file: str
    watchers: dict = field(default_factory=dict)  # surface -> Watcher
    ignore_rules: tuple = (set(), [], True)


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
               "host": socket.gethostname(), "v": WATCHDOG_VERSION, "event": event, **fields}
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
    """Check over ~16s whether a turn started; return (confirmed, post-state,
    post-tail) so failures keep their evidence. Codex can take well over a few
    seconds to show a running turn after /goal resume — a short window here
    mislabels successful resumes as unconfirmed (harmless for safety: the next
    poll sees the busy marker and re-arms, but it poisons the stats)."""
    state, tail = "", ""
    for attempt in range(4):
        time.sleep(VERIFY_WAIT)
        tail = read_tail(surface)
        state, _ = classify(tail)
        if state == "busy":
            return True, state, tail
    return False, state, tail


def confirm_pending(cfg: Config, watcher: Watcher) -> None:
    """A busy sighting after our send confirms the resume, even when the turn
    took too long to appear inside the verify window."""
    pending = watcher.pending
    if not pending:
        return
    watcher.pending = None
    since = round(time.time() - pending["stop_since"], 1) if pending["stop_since"] else None
    log(cfg, f"{watcher.surface}: resume confirmed late ({int(time.time() - pending['sent_at'])}s after send)")
    record(cfg, "resumed", surface=watcher.surface, title=watcher.title,
           command=pending["command"], attempt=pending["attempt"], lane=pending["lane"],
           since_stop_s=since, late=True)


def send_once(cfg: Config, watcher: Watcher, command: str, state: str, marker: str, lane: str) -> str:
    """Send one resume command and verify. Returns confirmed / unconfirmed / error."""
    log(cfg, f"{watcher.surface}: {state}; sending {command!r} because {marker!r} (attempt {watcher.actions_on_stop}, {lane} lane)")
    record(cfg, "action", surface=watcher.surface, title=watcher.title,
           state=state, marker=marker, command=command, attempt=watcher.actions_on_stop, lane=lane)
    now = time.time()
    try:
        send_literal(watcher.surface, command)
        # Our send may change the session state; a stop observed after it
        # counts as new evidence even if the on-screen marker text is identical.
        watcher.marker_seen = ""
    except ComposerContaminated as exc:
        log(cfg, f"{watcher.surface}: {exc}")
        record(cfg, "composer_race_aborted", surface=watcher.surface, command=command)
        notify("watchdog: composer race", f"{watcher.surface}: aborted a resume rather than submit a mixed draft")
        watcher.next_action_at = time.time() + 120
        # Nothing was submitted, so the stop is still standing: re-arm the
        # marker or the novelty gate would dead-letter this stop forever.
        watcher.marker_seen = ""
        return "error"
    except SubmitAmbiguous as exc:
        # The Enter keystroke may have landed. Do NOT re-arm the marker (a
        # duplicate command is worse than a paused stop); instead park a
        # pending confirmation so a started turn is still credited, and let
        # the composer guard defer any further sends until the draft clears.
        log(cfg, f"{watcher.surface}: {exc}")
        record(cfg, "submit_ambiguous", surface=watcher.surface, title=watcher.title, command=command)
        notify("watchdog: ambiguous submit", f"{watcher.surface}: Enter may have landed; not retrying automatically")
        watcher.pending = {"command": command, "attempt": watcher.actions_on_stop,
                           "lane": lane, "sent_at": now, "stop_since": watcher.stop_since,
                           "ambiguous": True}
        watcher.next_action_at = time.time() + SLOW_LANE_INTERVAL
        return "error"
    except Exception as exc:
        # Failures before Enter can only leave unsubmitted text behind, so
        # the stop still stands and the marker re-arms for the next poll.
        log(cfg, f"{watcher.surface}: send failed: {exc}")
        record(cfg, "send_error", surface=watcher.surface, command=command, error=str(exc))
        watcher.marker_seen = ""
        return "error"
    confirmed, post_state, post_tail = verify_resumed(watcher.surface)
    if confirmed:
        elapsed = round(now - watcher.stop_since, 1) if watcher.stop_since else None
        log(cfg, f"{watcher.surface}: resumed, turn is running")
        record(cfg, "resumed", surface=watcher.surface, title=watcher.title,
               command=command, attempt=watcher.actions_on_stop, lane=lane, since_stop_s=elapsed)
        return "confirmed"
    # The post-attempt screen is the evidence for why a resume missed:
    # wrong state read, client rejected the command, dialog in the way.
    log(cfg, f"{watcher.surface}: sent {command!r} but no turn is running (now: {post_state}); will re-check")
    record(cfg, "resume_unconfirmed", surface=watcher.surface, title=watcher.title,
           command=command, attempt=watcher.actions_on_stop, lane=lane,
           post_state=post_state, post_excerpt=tail_excerpt(post_tail))
    # Codex can take tens of seconds to show a running turn. If the session
    # goes busy later, that is this send working late — confirm it then.
    watcher.pending = {"command": command, "attempt": watcher.actions_on_stop,
                       "lane": lane, "sent_at": now, "stop_since": watcher.stop_since}
    return "unconfirmed"


def act(cfg: Config, watcher: Watcher, state: str, tail: str, marker: str = "") -> None:
    if is_ignored(cfg.ignore_rules, watcher.surface, watcher.title):
        return
    if watcher.last_seen_busy == 0 or time.time() - watcher.last_seen_busy > WITNESS_FRESHNESS:
        # Never witnessed busy (or not recently): almost certainly a
        # deliberately stopped or abandoned session, so leave it alone.
        fp = fingerprint(state, marker, tail)
        if fp != watcher.last_fingerprint:
            watcher.last_fingerprint = fp
            age = "never" if watcher.last_seen_busy == 0 else f"{int((time.time() - watcher.last_seen_busy) / 3600)}h ago"
            log(cfg, f"{watcher.surface}: {state} but last seen busy {age}; leaving it (witness rule)")
            record(cfg, "stale_stop_skipped", surface=watcher.surface, title=watcher.title,
                   state=state, marker=marker, last_seen_busy_age_s=(
                       None if watcher.last_seen_busy == 0 else round(time.time() - watcher.last_seen_busy, 1)),
                   fingerprint=fp, excerpt=tail_excerpt(tail))
        return
    now = time.time()
    pending = watcher.pending
    has_evidence = state in ("goal-paused", "capacity-stopped") and marker
    if pending and not pending.get("ambiguous"):
        # A command was (probably) submitted and no turn has been confirmed:
        # suppress ALL further sends while the confirmation window is open —
        # even if the visible marker text changes, which the novelty gate
        # would otherwise read as a new stop. A busy sighting confirms the
        # turn; evidence disappearing ends the episode; and if the window
        # lapses with no turn, the submission conclusively did not take, so
        # the stop becomes retryable again (the slow lane's whole job).
        if not has_evidence:
            watcher.pending = None
            watcher.marker_seen = ""
            log(cfg, f"{watcher.surface}: evidence gone with a pending submission; clearing it")
            record(cfg, "pending_cleared_no_evidence", surface=watcher.surface, title=watcher.title)
            return
        if now - pending["sent_at"] < SLOW_LANE_INTERVAL:
            return
        watcher.pending = None
        watcher.marker_seen = ""
        log(cfg, f"{watcher.surface}: no turn {SLOW_LANE_INTERVAL // 60}m after submission; the stop is retryable again")
        record(cfg, "pending_expired", surface=watcher.surface, title=watcher.title)
    if pending and pending.get("ambiguous") and not has_evidence:
        # The error evidence scrolled away: without a current signature this
        # stop is indistinguishable from a deliberate pause, and the evidence
        # gate outranks the pending retry. Drop it. (goal-paused-no-error
        # carries a state marker, not evidence.)
        watcher.pending = None
        watcher.marker_seen = ""
        log(cfg, f"{watcher.surface}: error evidence gone; dropping the ambiguous retry")
        record(cfg, "ambiguous_abandoned", surface=watcher.surface, title=watcher.title)
    if pending and pending.get("ambiguous") and has_evidence:
        # An earlier send may or may not have submitted. The composer resolves
        # it: our command sitting there untouched means Enter never landed and
        # pressing it now submits exactly our own command; a cleared composer
        # means the user tidied up and a fresh send is safe; user text means a
        # human is engaged, so defer.
        if now < watcher.next_action_at:
            return
        content = composer_content(tail)
        if content == pending["command"]:
            log(cfg, f"{watcher.surface}: ambiguous send left our command untouched in the composer; pressing Enter")
            record(cfg, "ambiguous_retry", surface=watcher.surface, title=watcher.title,
                   command=pending["command"], via="enter")
            try:
                cmux("send-key", "--surface", watcher.surface, "enter")
            except Exception as exc:
                log(cfg, f"{watcher.surface}: ambiguous retry failed: {exc}")
                record(cfg, "send_error", surface=watcher.surface, command=pending["command"], error=str(exc))
                watcher.next_action_at = now + SLOW_LANE_INTERVAL
                return
            time.sleep(1.0)
            # The composer is the witness again: if our command still sits
            # there, Enter did not take and the stop is still ambiguous.
            if composer_content(read_tail(watcher.surface)) == pending["command"]:
                pending["retries"] = pending.get("retries", 0) + 1
                if pending["retries"] >= 3:
                    log(cfg, f"{watcher.surface}: Enter never takes on this session; leaving it for a human")
                    record(cfg, "ambiguous_gave_up", surface=watcher.surface, title=watcher.title,
                           command=pending["command"])
                    notify("watchdog: cannot resume a session", f"{watcher.surface}: Enter never takes")
                    watcher.pending = None
                    watcher.next_action_at = now + SLOW_LANE_INTERVAL
                    return
                log(cfg, f"{watcher.surface}: Enter did not take; will retry")
                watcher.next_action_at = now + SLOW_LANE_INTERVAL
                return
            # The command left the composer: it was submitted. Keep a
            # late-confirmation pending and do NOT send again — a duplicate is
            # worse than waiting for the turn to appear.
            watcher.pending = {"command": pending["command"], "attempt": pending["attempt"],
                               "lane": pending["lane"], "sent_at": now,
                               "stop_since": pending["stop_since"]}
            confirmed, post_state, _post_tail = verify_resumed(watcher.surface)
            if confirmed:
                elapsed = round(now - pending["stop_since"], 1) if pending["stop_since"] else None
                log(cfg, f"{watcher.surface}: resumed via Enter retry, turn is running")
                record(cfg, "resumed", surface=watcher.surface, title=watcher.title,
                       command=pending["command"], attempt=pending["attempt"], lane=pending["lane"],
                       since_stop_s=elapsed)
                watcher.pending = None
                return
            log(cfg, f"{watcher.surface}: command submitted; awaiting the turn (no further sends for this stop)")
            record(cfg, "submitted_awaiting_turn", surface=watcher.surface, title=watcher.title,
                   command=pending["command"], attempt=pending["attempt"])
            watcher.next_action_at = now + SLOW_LANE_INTERVAL
            return
        if composer_has_text(tail):
            log(cfg, f"{watcher.surface}: ambiguous send plus user text in the composer; deferring to the human")
            watcher.next_action_at = now + 120
            return
        # The composer is empty — which is exactly what a SUCCESSFUL
        # submission looks like too, so this is not proof the user cleared
        # anything. A fresh send here could duplicate the command. Treat the
        # command as possibly submitted: park a late-confirmation pending and
        # send nothing further for this stop. If no turn ever appears the stop
        # stays parked and visible, which is the safe direction.
        log(cfg, f"{watcher.surface}: composer empty after ambiguous send; treating as possibly submitted, not resending")
        record(cfg, "ambiguous_assumed_submitted", surface=watcher.surface, title=watcher.title,
               command=pending["command"], attempt=pending["attempt"])
        notify("watchdog: ambiguous submit", f"{watcher.surface}: command may or may not have submitted; check the session")
        watcher.pending = {"command": pending["command"], "attempt": pending["attempt"],
                           "lane": pending["lane"], "sent_at": now,
                           "stop_since": pending["stop_since"]}
        watcher.next_action_at = now + SLOW_LANE_INTERVAL
        return

    fp = fingerprint(state, marker, tail)
    new_episode = fp != watcher.last_fingerprint
    if new_episode:
        # A new stop episode: reset the per-stop bookkeeping. The episode
        # boundary re-arms marker novelty too — the same marker text on a
        # differently-shaped screen is a different stop.
        watcher.last_fingerprint = fp
        watcher.actions_on_stop = 0
        watcher.next_action_at = 0.0
        watcher.parked = False
        watcher.stop_since = now
        watcher.pending = None
        watcher.marker_seen = ""
        watcher.skip_logged = False
    if state in ("goal-paused", "capacity-stopped"):
        # Marker novelty: this exact marker instance was already evaluated
        # within this stop episode and not cleared by a busy turn or one of
        # our sends, so it is stale scrollback, not a new stop. A deliberate
        # pause under an old error lands here. (goal-paused-no-error carries
        # a state marker, not evidence, and is handled below.)
        if watcher.marker_seen == marker:
            if not watcher.skip_logged:
                watcher.skip_logged = True
                log(cfg, f"{watcher.surface}: {state} but evidence {marker!r} is not new; leaving it")
                record(cfg, "stale_evidence_skipped", surface=watcher.surface, title=watcher.title,
                       state=state, marker=marker, fingerprint=fp, excerpt=tail_excerpt(tail))
            return
        watcher.marker_seen = marker
    if state == "goal-paused-no-error":
        if not watcher.skip_logged:
            watcher.skip_logged = True
            log(cfg, f"{watcher.surface}: goal paused without an error on screen; deliberate pause, leaving it")
            record(cfg, "deliberate_pause_skipped", surface=watcher.surface, title=watcher.title,
                   fingerprint=fp, excerpt=tail_excerpt(tail))
        return
    command = "continue" if state == "capacity-stopped" else "/goal resume"
    if new_episode:
        log(cfg, f"{watcher.surface}: stop detected ({state}), evidence {marker!r}")
        record(cfg, "stop_detected", surface=watcher.surface, title=watcher.title,
               state=state, marker=marker, fingerprint=fp, excerpt=tail_excerpt(tail))
    if now < watcher.next_action_at:
        return
    if composer_has_text(tail):
        log(cfg, f"{watcher.surface}: {state} but composer has unsent text; leaving it alone")
        record(cfg, "composer_blocked", surface=watcher.surface, state=state)
        watcher.next_action_at = now + 60
        return
    if cfg.dry_run:
        lane = "fast" if watcher.actions_on_stop < MAX_ACTIONS_PER_STOP else "slow"
        log(cfg, f"{watcher.surface}: DRY-RUN would send {command!r} ({lane} lane)")
        return

    if watcher.actions_on_stop >= MAX_ACTIONS_PER_STOP:
        # Slow lane: one gentle send per poll until the storm passes.
        if not watcher.parked:
            watcher.parked = True
            log(cfg, f"{watcher.surface}: slow lane after {watcher.actions_on_stop} quick attempts; retrying every {SLOW_LANE_INTERVAL // 60}m")
            record(cfg, "slow_lane", surface=watcher.surface, attempts=watcher.actions_on_stop)
            notify("watchdog: session in slow lane", f"{watcher.surface} keeps failing to resume")
        watcher.next_action_at = now + SLOW_LANE_INTERVAL
        watcher.actions_on_stop += 1
        send_once(cfg, watcher, command, state, marker, "slow")
        return

    # Fast lane: work the whole backoff ladder inline, so a detected stop gets
    # full-speed retrying even though steady-state polls are minutes apart.
    # Every attempt re-reads the screen first: a session that starts on its
    # own, a state change, or the user typing aborts the ladder immediately.
    while watcher.actions_on_stop < MAX_ACTIONS_PER_STOP:
        watcher.actions_on_stop += 1
        outcome = send_once(cfg, watcher, command, state, marker, "fast")
        if outcome == "confirmed":
            return
        if outcome == "error":
            # A composer race means the user is typing right now; a transport
            # error means cmux is unhappy. Either way the ladder stops here
            # and the error path's own retry timing applies.
            return
        if watcher.actions_on_stop >= MAX_ACTIONS_PER_STOP:
            break
        backoff = min(FAST_BACKOFF_CAP, 5 * (2 ** (watcher.actions_on_stop - 1)))
        if outcome == "unconfirmed":
            # Never pile a second send into a slow-starting turn.
            backoff = max(backoff, CONFIRM_GRACE)
        time.sleep(backoff)
        tail = read_tail(watcher.surface)
        state, marker = classify(tail)
        if state == "busy":
            confirm_pending(cfg, watcher)
            return
        if state not in ("goal-paused", "capacity-stopped") or composer_has_text(tail):
            log(cfg, f"{watcher.surface}: stop changed shape mid-ladder (now {state}); pausing ladder")
            watcher.next_action_at = time.time() + 60
            return
    # Ladder spent without a confirmed resume: hand off to the slow lane.
    watcher.parked = True
    log(cfg, f"{watcher.surface}: slow lane after {watcher.actions_on_stop} quick attempts; retrying every {SLOW_LANE_INTERVAL // 60}m")
    record(cfg, "slow_lane", surface=watcher.surface, attempts=watcher.actions_on_stop)
    notify("watchdog: session in slow lane", f"{watcher.surface} keeps failing to resume")
    watcher.next_action_at = time.time() + SLOW_LANE_INTERVAL


def discover_codex_surfaces(cfg: Config, witnesses: dict) -> None:
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
                if is_ignored(cfg.ignore_rules, sid, title.strip()):
                    log(cfg, f"discovery: ignoring excluded session {sid} ({title.strip()})")
                    continue
                cfg.watchers[sid] = Watcher(surface=sid, title=title.strip(),
                                            last_seen_busy=witnesses.get(sid, 0.0))
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
    ambiguous = [e for e in events if e.get("event") == "submit_ambiguous"]
    ambiguous_retries = [e for e in events if e.get("event") == "ambiguous_retry"]
    blocked = [e for e in events if e.get("event") == "composer_blocked"]
    deliberate = [e for e in events if e.get("event") == "deliberate_pause_skipped"]
    stalls = [e for e in events if e.get("event") == "stall_observed"]
    stale = [e for e in events if e.get("event") == "stale_stop_skipped"]
    stale_ev = [e for e in events if e.get("event") == "stale_evidence_skipped"]
    primed = [e for e in events if e.get("event") == "primed_stopped"]
    slow = [e for e in events if e.get("event") == "slow_lane"]

    print(f"events: {len(events)}  (since {events[0].get('ts', '?')})")

    # Version segmentation: behavior changes across versions, so aggregate
    # per version instead of mixing policies into one number.
    versions = {}
    for e in events:
        versions.setdefault(e.get("v", "pre-versioning"), []).append(e)
    for version, vevents in versions.items():
        v_stops = sum(1 for e in vevents if e.get("event") == "stop_detected")
        v_actions = sum(1 for e in vevents if e.get("event") == "action")
        v_resumed = sum(1 for e in vevents if e.get("event") == "resumed")
        v_unconf = sum(1 for e in vevents if e.get("event") == "resume_unconfirmed")
        print(f"  v{version}: {vevents[0].get('ts', '?')} → {vevents[-1].get('ts', '?')} — "
              f"{v_stops} stops, {v_actions} actions, {v_resumed} resumed, {v_unconf} unconfirmed")
    print(f"stops detected: {len(stops)}")
    by_state = {}
    for e in stops:
        by_state[e.get("state", "?")] = by_state.get(e.get("state", "?"), 0) + 1
    for state, count in sorted(by_state.items()):
        print(f"  {state}: {count}")
    print(f"resume actions: {len(actions)}  "
          f"(goal resume: {sum(1 for e in actions if e.get('command') == '/goal resume')}, "
          f"continue: {sum(1 for e in actions if e.get('command') == 'continue')})")
    late = sum(1 for e in resumed if e.get("late"))
    print(f"resumed OK: {len(resumed)} ({late} confirmed late)  unconfirmed: {len(unconfirmed)}  send errors: {len(send_errors)}  ambiguous submits: {len(ambiguous)} (resolved by retry: {len(ambiguous_retries)})")
    if resumed:
        attempts = sorted(e.get("attempt", 0) for e in resumed)
        latencies = sorted(e.get("since_stop_s") for e in resumed if e.get("since_stop_s") is not None)
        print(f"attempts to resume: median {attempts[len(attempts) // 2]}, max {attempts[-1]}")
        if latencies:
            mid = len(latencies) // 2
            median = latencies[mid] if len(latencies) % 2 else (latencies[mid - 1] + latencies[mid]) / 2
            print(f"time-to-resume: median {median}s, max {latencies[-1]}s")
    print(f"composer-blocked deferrals: {len(blocked)}  slow-lane entries: {len(slow)}  deliberate pauses left alone: {len(deliberate)}  stalls observed (never acted on): {len(stalls)}  stale stops skipped (witness rule): {len(stale)}  stale evidence skipped: {len(stale_ev)}  primed-on-stopped: {len(primed)}")

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
    # lane ever lands a resume — the input for tuning the 5-minute cool-off.
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
    parser.add_argument("--interval", type=float, default=300.0, help="seconds between steady-state polls (default 300; a detected stop is worked at full speed inline)")
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
    witnesses = load_witnesses(lambda message: log(cfg, message))
    for surface in args.surface:
        cfg.watchers[surface] = Watcher(surface=surface, last_seen_busy=witnesses.get(surface, 0.0))
    log(cfg, f"watchdog v{WATCHDOG_VERSION} starting: poll {cfg.interval}s, fast ladder x{MAX_ACTIONS_PER_STOP} "
             f"(backoff 5s->{FAST_BACKOFF_CAP}s, grace {CONFIRM_GRACE}s), slow lane {SLOW_LANE_INTERVAL}s, "
             f"witness freshness {WITNESS_FRESHNESS // 3600}h, dry_run={cfg.dry_run}, all_codex={args.all_codex}")
    record(cfg, "watchdog_started", interval_s=cfg.interval, fast_attempts=MAX_ACTIONS_PER_STOP,
           slow_lane_s=SLOW_LANE_INTERVAL, witness_freshness_h=WITNESS_FRESHNESS // 3600,
           dry_run=cfg.dry_run, all_codex=args.all_codex, pinned=list(cfg.watchers))

    last_discovery = 0.0
    witnesses_dirty = False
    last_witness_save = 0.0
    while True:
        cfg.ignore_rules = load_ignore_rules()
        if args.all_codex and time.time() - last_discovery >= DISCOVERY_INTERVAL:
            discover_codex_surfaces(cfg, witnesses)
            last_discovery = time.time()
        for surface, watcher in list(cfg.watchers.items()):
            try:
                tail = read_tail(watcher.surface)
                state, marker = classify(tail)
                if state == "busy":
                    watcher.marker_seen = ""
                    confirm_pending(cfg, watcher)
                    # A running turn proves the last resume worked; re-arm.
                    if watcher.last_seen_busy == 0:
                        record(cfg, "witnessed_busy", surface=watcher.surface, title=watcher.title)
                    watcher.last_seen_busy = time.time()
                    watcher.last_fingerprint = ""
                    watcher.actions_on_stop = 0
                    watcher.parked = False
                    watcher.stalled_polls = 0
                    witnesses_dirty = True
                    continue
                if state in ("idle", "goal-stalled-candidate") and watcher.pending:
                    # The stop settled without a confirmable turn (a short
                    # turn can complete between 5-minute polls, or the session
                    # was cleaned up): nothing may suppress a future stop.
                    watcher.pending = None
                    watcher.marker_seen = ""
                    log(cfg, f"{watcher.surface}: session settled ({state}) with a pending submission; clearing it")
                    record(cfg, "pending_cleared_idle", surface=watcher.surface, title=watcher.title, state=state)
                if state == "goal-stalled-candidate":
                    # Between-turn gaps are seconds; only a persistent idle
                    # "pursuing goal" footer is a real stall. Observe-only:
                    # a stall has no error signature, and we never resume
                    # anything without one — but knowing stalls happen informs
                    # future policy.
                    watcher.stalled_polls += 1
                    if watcher.stalled_polls >= STALL_POLLS_REQUIRED:
                        fp = fingerprint("goal-stalled", marker, tail)
                        if fp != watcher.last_fingerprint:
                            watcher.last_fingerprint = fp
                            log(cfg, f"{watcher.surface}: goal footer active but no turn running (stall; observe-only)")
                            record(cfg, "stall_observed", surface=watcher.surface, title=watcher.title,
                                   fingerprint=fp, excerpt=tail_excerpt(tail))
                    continue
                watcher.stalled_polls = 0
                if not watcher.primed:
                    # First sighting of a surface records its full episode
                    # state without acting, so a stop we never saw begin is
                    # never mistaken for a fresh one. The fingerprint must be
                    # primed too: otherwise the next identical poll reads as a
                    # "new episode" and the reset would wipe marker_seen,
                    # bypassing this guard entirely.
                    watcher.primed = True
                    if marker:
                        watcher.marker_seen = marker
                        watcher.last_fingerprint = fingerprint(state, marker, tail)
                        log(cfg, f"{watcher.surface}: primed on an already-stopped session ({state}); observing only")
                        record(cfg, "primed_stopped", surface=watcher.surface, title=watcher.title,
                               state=state, marker=marker)
                        continue
                if state in ("goal-paused", "goal-paused-no-error", "capacity-stopped"):
                    act(cfg, watcher, state, tail, marker)
            except Exception as exc:
                if surface in args.surface:
                    log(cfg, f"{watcher.surface}: poll error: {exc}")
                else:
                    # Discovered surface went away (tab closed); drop it.
                    log(cfg, f"discovery: stopped watching {surface} ({exc})")
                    del cfg.watchers[surface]
        if witnesses_dirty and time.time() - last_witness_save >= 60:
            save_witnesses(cfg.watchers)
            witnesses_dirty = False
            last_witness_save = time.time()
        time.sleep(cfg.interval)


if __name__ == "__main__":
    sys.exit(main())
