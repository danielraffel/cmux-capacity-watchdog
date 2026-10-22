#!/usr/bin/env python3
"""Watch cmux agent sessions and resume ones that stopped on provider-capacity errors.

State-aware: a session whose goal paused gets "/goal resume"; a session with no
goal that stopped on a capacity-class error gets "continue". Resume is
evidence-gated: every action requires a known transient-error signature on
screen (capacity/overload/stream failure). Sessions that stopped any other way
— user pause, user interrupt, clean finish, deliberate abandonment — are never
touched; nor are busy turns. An unsent draft in the composer blocks action, with
one exception: a draft that exactly matches the resume command is submitted with
Enter (it is either our own orphaned send from before a restart or the user's
identical intent), bounded to a few attempts.

Steady-state polls are cheap (every minute by default). Recovery is
round-robin: each visit makes at most one attempt per session, the backoff
schedule persists per session in next_action_at, and the loop wakes at the
earlier of the next poll or the earliest due retry — so the fast lane keeps
its 5s→60s exponential spacing without any session blocking the global scan.

Usage:
    cmux-capacity-watchdog.py --all [--interval 60] [--dry-run] [--log FILE]
    cmux-capacity-watchdog.py --problems [HOURS] | --report | --check-rules | --classify FILE
"""

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

# The cmux CLIs. Each CLI talks only to its own app, and the release and
# nightly apps can run side by side, so every running app is enumerated
# through its own CLI and each surface remembers which CLI owns it.
# CMUX_BIN pins a single CLI.
CMUX_CANDIDATES = (
    "/Applications/cmux NIGHTLY.app/Contents/Resources/bin/cmux",
    "/Applications/cmux.app/Contents/Resources/bin/cmux",
)


def running_clis() -> list:
    """CLIs whose app is running (ps, not pgrep: pgrep misses some app
    processes); every installed CLI when none is detectably running."""
    override = os.environ.get("CMUX_BIN")
    if override:
        return [override]
    installed = [path for path in CMUX_CANDIDATES if os.path.exists(path)]
    try:
        commands = subprocess.run(["/bin/ps", "-axo", "command"], capture_output=True,
                                  text=True, timeout=10).stdout.splitlines()
    except Exception:
        commands = []
    running = [path for path in installed
               if any(line.startswith(path.split("/Contents/", 1)[0] + "/Contents/MacOS/cmux")
                      for line in commands)]
    return running or installed or [CMUX_CANDIDATES[-1]]


CMUX = running_clis()[0]
SURFACE_CLI = {}  # surface id -> the CLI of the app that owns it
# --dry-run: no keystroke ever reaches a tab, whichever code path asks.
DRY_RUN = False
INSTANCE_LOCK = None  # held for the process lifetime

# Bump on EVERY behavior change. Every stats event carries this version, so
# logs and reports stay interpretable across policy changes; the git history
# maps versions to commits.
WATCHDOG_VERSION = "2026-10-22.8"

# Per-agent rules: how to recognise the agent's screen, when it is busy, what
# its composer looks like, which on-screen errors are resume evidence, and
# what to type. Everything an operator tunes lives here, and the rules file
# (RULES_FILE) can replace or extend any key without a code change or a
# restart. Grow evidence lists from the excerpts captured in stats, never
# speculatively.
DEFAULT_PROFILES = {
    "codex": {
        "enabled": True,
        # A surface counts as a Codex session when its composer placeholder or
        # model slug is visible. Works on busy turns too.
        "detect_contains": ["ask codex to do anything", "gpt-"],
        "prompt_glyph": "›",
        "composer_needs_border": False,
        # Known empty-composer placeholders by client.
        "placeholders": ["ask codex to do anything", "ask kimi", "type a message", "ask anything"],
        # A turn is running when any of these appear in the evidence window.
        "busy_contains": ["esc to interrupt"],
        "busy_regex": [],
        "retrying_regex": [],
        # Codex words it "Goal stalled (/goal resume)"; Kimi and others say
        # "goal paused".
        "goal_paused_contains": ["goal paused", "goal stalled"],
        "goal_command": "/goal resume",
        "continue_command": "continue",
        "stall_contains": "pursuing goal",
        # The current stop's error sits just above the composer; the last
        # few raw lines are the evidence window.
        "evidence": "tail",
        # Transient provider problems worth retrying. User interrupts (Esc)
        # and clean completions never match these.
        "evidence_contains": [
            "selected model is at capacity",
            "model is at capacity",
            "server_is_overloaded",
            "servers are currently overloaded",
            "stream disconnected before completion",
        ],
        "evidence_regex": [],
        # Fast attempts before the slow lane, and the slow lane's spacing.
        "fast_attempts": 12,
        "slow_lane_interval": 300,
        "tail_lines": 30,
        # Feature switches, each enabled on its own: typing the resume
        # command after a transient stop, and answering permission prompts
        # that a prompt rule covers.
        "resume": True,
        "answer_prompts": False,
        "prompt_rules": [],
    },
    "claude": {
        "enabled": True,
        # Claude Code draws its composer as a "❯" line between two rules. Its
        # selection dialogs also use "❯" but never inside the rules. A
        # permission prompt hides the composer; its last line names Claude
        # (see detect_agent).
        "detect_contains": [],
        "detect_composer": True,
        "prompt_glyph": "❯",
        "composer_needs_border": True,
        "placeholders": ["press up to edit queued messages", "try \""],
        "busy_contains": ["esc to interrupt"],
        # The spinner line: a glyph, a verb and an ellipsis ("· Envisioning…").
        # A finished turn reads "✻ Baked for 2m 57s · done", with no ellipsis.
        "busy_regex": [r"^\s*[·✢✳✶✻✽*]\s+\S+…"],
        # Claude's own retry loop. Not a running turn (a resume that only
        # reached this state did not work) and not a busy witness.
        "retrying_regex": [r"retrying in \d+s · attempt \d+/\d+"],
        "goal_paused_contains": [],
        "goal_command": "",
        "continue_command": "continue",
        "stall_contains": "",
        # Full-screen Claude leaves blank lines between the transcript and the
        # composer, so the raw tail can miss the error entirely. Evidence is
        # the last transcript block above the composer: Claude's final
        # message must itself be the error. An old error followed by a
        # normal reply never qualifies.
        "evidence": "last_block",
        "block_glyphs": ["⏺", "●"],
        "evidence_contains": [],
        "evidence_regex": [
            r"^[⏺●] API Error: 5\d\d\b",
            r"^[⏺●] API Error: .*overloaded",
            r"^[⏺●] API Error: (Connection lost|The response stopped arriving|Your computer went to sleep)",
            r"^[⏺●] (API Error: )?Request timed out",
        ],
        # Claude already retries ten times internally before it shows the
        # error, so a few fast attempts, then a gentle slow lane.
        "fast_attempts": 3,
        "slow_lane_interval": 900,
        # Full-screen Claude can sit its last message far above the composer
        # in a tall window with a short transcript.
        "tail_lines": 80,
        "resume": True,
        # Off unless the rules file turns it on.
        "answer_prompts": False,
        # Claude asks before saving its own memory notes when its config
        # folder is not the one holding them (pooled Subrouter sessions use a
        # proxy config folder). A rule answers only when the file resolves to
        # <root>/projects/<project>/memory/<name>.md under a memory root.
        "prompt_rules": [
            {"question": r"^Do you want to (create|overwrite|make this edit to|make these edits to) \S+\?$",
             "files": "claude-memory", "answer": "1",
             "note": "Claude saving one of its own memory notes."},
            # Claude asks this for a "sensitive" file (a memory note reached
            # through Subrouter's symlink) and for shell commands alike, so
            # the screen can't tell them apart. It is answered only when the
            # session's single pending tool call is a file edit of a memory
            # note; a pending shell command is never answered.
            {"question": r"^Do you want to proceed\?$",
             "files": "claude-memory", "answer": "1",
             "note": "Claude editing one of its own memory notes (sensitive-file prompt)."},
        ],
        "memory_roots": ["~/.claude", "~/.subrouter/codex/claude-proxy/*"],
        # When Claude offers "Yes, and always allow access to <folder>" and
        # every folder is a memory folder, choose it so Claude stops asking
        # for later notes. Never chosen when that option also changes the
        # session's mode or allows "similar commands".
        "always_allow": True,
        # Claude's multiple-choice questions (AskUserQuestion): pick the
        # option Claude marked "(Recommended)" once the question has waited
        # this many seconds, so a person can answer first. Needs
        # answer_prompts. A question with no single Recommended option is
        # left for a person.
        "answer_recommended": True,
        "recommended_delay": 120,
    },
}

# Operator overrides, re-read on every cycle. Shape:
#   {"agents": {"claude": {"evidence_regex_add": ["..."], "enabled": true},
#               "codex":  {"slow_lane_interval": 600}}}
# A key replaces the default; a "<key>_add" key appends to a list default.
# A malformed file keeps the last good rules and says so in the log.
RULES_FILE = os.path.expanduser("~/.config/cmux-capacity-watchdog/rules.json")

DISCOVERY_INTERVAL = 300.0  # seconds between scans for new sessions
# Enough lines for the Claude composer frame plus its footer and status line.
DISCOVERY_LINES = 14


@dataclass
class Profile:
    name: str
    rules: dict

    def __getattr__(self, key):
        try:
            return self.__dict__["rules"][key]
        except KeyError:
            raise AttributeError(key) from None

    def matches_any(self, text: str, contains_key: str, regex_key: str) -> str:
        """Return the first matching marker (contains, then regex), or ""."""
        lowered = text.lower()
        for marker in self.rules.get(contains_key, []):
            if marker in lowered:
                return marker
        for pattern in self.rules.get(regex_key, []):
            for line in text.splitlines():
                if re.search(pattern, line.strip(), re.IGNORECASE):
                    return pattern
        return ""


def build_profiles(overrides: dict) -> dict:
    profiles = {}
    for name, defaults in DEFAULT_PROFILES.items():
        rules = {key: (list(value) if isinstance(value, list) else value) for key, value in defaults.items()}
        for key, value in (overrides.get(name) or {}).items():
            if key.endswith("_add") and isinstance(rules.get(key[:-4]), list):
                rules[key[:-4]].extend(value)
            else:
                rules[key] = value
        profiles[name] = Profile(name=name, rules=rules)
    return profiles


def validate_overrides(data) -> dict:
    """The rules file's agent overrides, checked against the defaults: known
    agents and keys only, each value the same type as its default (lists of
    strings stay lists, booleans stay booleans), patterns that compile, and
    well-formed prompt rules. Raises ValueError naming the first problem."""
    if not isinstance(data, dict):
        raise ValueError("the file must hold a JSON object")
    unknown = set(data) - {"agents"}
    if unknown:
        raise ValueError(f"unknown top-level key {sorted(unknown)[0]!r}")
    agents = data.get("agents", {})
    if not isinstance(agents, dict):
        raise ValueError('"agents" must be an object')
    for agent, overrides in agents.items():
        if agent not in DEFAULT_PROFILES:
            raise ValueError(f"unknown agent {agent!r} (known: {', '.join(DEFAULT_PROFILES)})")
        if not isinstance(overrides, dict):
            raise ValueError(f"{agent}: settings must be an object")
        defaults = DEFAULT_PROFILES[agent]
        for key, value in overrides.items():
            base = key[:-4] if key.endswith("_add") else key
            if base not in defaults:
                raise ValueError(f"{agent}: unknown setting {key!r}")
            default = defaults[base]
            if key.endswith("_add") and not isinstance(default, list):
                raise ValueError(f"{agent}: {key!r} can only extend a list setting")
            if isinstance(default, bool):
                ok = isinstance(value, bool)
            elif isinstance(default, int):
                ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
            elif isinstance(default, list):
                ok = isinstance(value, list)
            else:
                ok = isinstance(value, type(default))
            if not ok:
                raise ValueError(f"{agent}: {key!r} must be {type(default).__name__}")
            if base == "prompt_rules":
                for rule in value:
                    if not isinstance(rule, dict) or not isinstance(rule.get("question"), str) \
                            or not isinstance(rule.get("answer"), str) or not rule["answer"] \
                            or rule.get("files") != "claude-memory":
                        raise ValueError(f'{agent}: each prompt rule needs "question", "answer" '
                                         f'and "files": "claude-memory"')
                    re.compile(rule["question"])
            elif isinstance(default, list):
                if not all(isinstance(item, str) for item in value):
                    raise ValueError(f"{agent}: {key!r} must be a list of strings")
                if "regex" in base:
                    for pattern in value:
                        re.compile(pattern)
    return agents


class RulesLoader:
    """Reload RULES_FILE when it changes; keep the last good rules otherwise."""

    def __init__(self, path: str = RULES_FILE):
        self.path = path
        self.mtime = None
        self.profiles = build_profiles({})
        self.error = ""

    def load(self, log_fn=print) -> dict:
        try:
            mtime = os.stat(self.path).st_mtime
        except FileNotFoundError:
            if self.mtime is not None:
                log_fn(f"rules file removed; using built-in rules")
                self.profiles, self.mtime = build_profiles({}), None
            return self.profiles
        except OSError as exc:
            log_fn(f"WARNING: rules file unreadable ({exc}); keeping the last good rules")
            return self.profiles
        if mtime == self.mtime:
            return self.profiles
        self.mtime = mtime
        try:
            with open(self.path) as handle:
                data = json.load(handle)
            agents = validate_overrides(data)
            self.profiles = build_profiles(agents)
            self.error = ""
            log_fn(f"rules loaded from {self.path}")
        except Exception as exc:
            self.error = str(exc)
            log_fn(f"WARNING: rules file invalid ({exc}); keeping the last good rules")
        return self.profiles


PROFILES = build_profiles({})

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
    except (OSError, UnicodeDecodeError) as exc:
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
ORPHAN_ENTER_MAX = 3            # bounded Enter retries for a composer draft that exactly matches the resume command


def cmux(*args: str, cli: str = "") -> str:
    """Run a cmux CLI command. A --surface command goes to the CLI of the app
    that owns that surface. A CLI that answers with an error message (some
    builds exit 0 with "Error: ..." for an unknown surface) raises, so a
    wrong-app read can never pass for an idle screen."""
    if DRY_RUN and args and args[0] in ("send", "send-key"):
        return ""
    if not cli and "--surface" in args:
        cli = SURFACE_CLI.get(args[args.index("--surface") + 1], "")
    # An inherited socket path steers any CLI to one app's socket; each CLI
    # must reach its own app, as it does under launchd. Credentials
    # (CMUX_SOCKET_PASSWORD, a capability) pass through.
    env = {key: value for key, value in os.environ.items() if key not in ("CMUX_SOCKET", "CMUX_SOCKET_PATH")}
    result = subprocess.run([cli or CMUX, *args], capture_output=True, text=True, timeout=30, env=env)
    cli_error = result.stdout.startswith("Error: ") and result.stdout.strip().count("\n") == 0
    if result.returncode != 0 or cli_error:
        raise RuntimeError(f"cmux {' '.join(args)}: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def read_tail(surface: str, lines: int = 30) -> str:
    return cmux("read-screen", "--surface", surface, "--lines", str(lines))


def is_rule(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= 20 and set(stripped) == {"─"}


def bordered_composer_index(lines: list) -> int:
    """Index of the last "❯" line framed by horizontal rules above and below
    (Claude's composer), or -1. Multi-line drafts sit above the lower rule."""
    for index in range(len(lines) - 1, 0, -1):
        if lines[index].strip().startswith("❯") and is_rule(lines[index - 1]):
            if any(is_rule(line) for line in lines[index + 1:index + 8]):
                return index
    return -1


def composer_lines(tail: str, profile: Profile) -> list:
    """Composer lines, last first. Codex: every "›" line. Claude: the one
    framed composer (selection dialogs use "❯" too, never framed)."""
    lines = tail.splitlines()
    if profile.composer_needs_border:
        index = bordered_composer_index(lines)
        return [lines[index]] if index >= 0 else []
    return [line for line in reversed(lines) if line.strip().startswith(profile.prompt_glyph)]


def composer_content(tail: str, profile: Profile = None) -> str:
    """The text currently in the composer (best-effort, last composer line).

    Spinner overlays (braille glyphs, U+2800–U+28FF) can bleed into the
    composer line in a screen read; they are display artifacts, not draft
    text, so strip them before comparing.
    """
    profile = profile or PROFILES["codex"]
    for line in composer_lines(tail, profile):
        content = line.strip()[len(profile.prompt_glyph):].strip()
        content = "".join(c for c in content if not 0x2800 <= ord(c) <= 0x28FF)
        return content.strip()
    return ""


class ComposerContaminated(Exception):
    """The composer changed around our send; aborted before Enter."""


class SubmitAmbiguous(Exception):
    """Enter may have landed before the failure — never auto-retry this stop."""


def send_literal(surface: str, text: str, profile: Profile = None) -> None:
    """Send control input (slash command or plain prompt) at column zero.

    Per the tab-coordination rules: slash commands are client control input and
    must never go through an attributing peer-message path; Enter follows only
    after the paste-debounce window — and only when the composer still holds
    exactly our text. If the user started typing in the gap, our text is
    backed out when it is a clean suffix, and the send aborts either way.
    """
    cmux("send", "--surface", surface, text)
    time.sleep(1.2)
    content = composer_content(read_tail(surface), profile)
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


def composer_has_text(tail: str, profile: Profile = None) -> bool:
    profile = profile or PROFILES["codex"]
    for line in composer_lines(tail, profile):
        content = line.strip()[len(profile.prompt_glyph):].strip()
        if not content:
            continue
        if any(content.lower().startswith(p) for p in profile.placeholders):
            continue
        return True
    return False


# The current stop's error always sits just above the composer; old errors
# scroll out of the viewport. Matching against the whole tail would let stale
# scrollback qualify as evidence for a stop that happened hours later (e.g. a
# deliberate pause long after a capacity error), so only the last lines count.
EVIDENCE_LINES = 12
# Claude: how many non-empty transcript lines above the composer to inspect
# for the spinner and the final message block.
TRANSCRIPT_LINES = 8


def transcript_above_composer(tail: str) -> list:
    """Non-empty lines between the transcript and Claude's framed composer,
    oldest first, with the composer's upper rule excluded."""
    lines = tail.splitlines()
    index = bordered_composer_index(lines)
    if index < 0:
        return []
    above = [line for line in lines[:index - 1] if line.strip()]
    return above[-TRANSCRIPT_LINES:]


def last_block(lines: list, glyphs: list) -> str:
    """The last transcript block that starts with one of the message glyphs,
    joined with its continuation lines. Status lines (✻ …, ⎿ tips) end it.
    A user prompt ("❯ …") after the last message means the user has spoken
    since, so there is no current agent message and the result is ""."""
    for start in range(len(lines) - 1, -1, -1):
        if lines[start].strip().startswith("❯"):
            return ""
        if any(lines[start].strip().startswith(g) for g in glyphs):
            block = [lines[start].strip()]
            for line in lines[start + 1:]:
                if not line.startswith("  ") or line.strip()[:1] in ("✻", "⎿", "❯"):
                    break
                block.append(line.strip())
            return " ".join(block)
    return ""


def classify(tail: str, profile: Profile = None) -> tuple:
    """Return (state, matched_marker). The marker is recorded in stats so we
    can see which failure signatures dominate and which we miss."""
    profile = profile or PROFILES["codex"]
    window = "\n".join(tail.splitlines()[-EVIDENCE_LINES:])
    if profile.evidence == "last_block":
        transcript = transcript_above_composer(tail)
        status = "\n".join(transcript[-3:] + [window])
        if profile.matches_any(status, "", "retrying_regex"):
            return "retrying", ""
        if profile.matches_any(window, "busy_contains", "") or \
                profile.matches_any("\n".join(transcript[-3:]), "", "busy_regex"):
            return "busy", ""
        block = last_block(transcript, profile.block_glyphs)
        marker = profile.matches_any(block, "evidence_contains", "evidence_regex") if block else ""
        if marker:
            return "capacity-stopped", marker
        return "idle", ""
    lowered = window.lower()
    if profile.matches_any(window, "busy_contains", "busy_regex"):
        return "busy", ""
    if profile.matches_any(window, "", "retrying_regex"):
        return "retrying", ""
    for marker in profile.goal_paused_contains:
        if marker in lowered:
            # A paused goal is only a candidate when a transient error is
            # visible too — users pause goals deliberately all the time, and
            # a deliberate pause has no error text on screen.
            cap = profile.matches_any(window, "evidence_contains", "evidence_regex")
            if cap:
                return "goal-paused", cap
            return "goal-paused-no-error", marker
    marker = profile.matches_any(window, "evidence_contains", "evidence_regex")
    if marker:
        return "capacity-stopped", marker
    if profile.stall_contains and profile.stall_contains in lowered:
        # Footer claims an active goal but no turn is running: a silent stall
        # the client never surfaced as an error. Needs persistence across
        # polls before acting (between-turn gaps are seconds).
        return "goal-stalled-candidate", "pursuing goal (idle)"
    return "idle", ""


def screen_matches(profile: Profile, tail: str) -> bool:
    if profile.rules.get("detect_composer") and bordered_composer_index(tail.splitlines()) >= 0:
        return True
    lowered = tail.lower()
    return any(marker in lowered for marker in profile.detect_contains)


def discovery_tail(surface: str) -> str:
    """A short read for agent detection, or a long one when the screen ends
    on a multiple-choice question, whose tab bar sits well above its footer."""
    tail = read_tail(surface, lines=DISCOVERY_LINES)
    last = next((line.strip().lower() for line in reversed(tail.splitlines()) if line.strip()), "")
    if "enter to select" in last and "esc to cancel" in last:
        tail = read_tail(surface, lines=120)
    return tail


def detect_agent(tail: str, profiles: dict) -> str:
    """Which enabled agent this screen belongs to, or "". Claude's framed
    composer is checked first: it is structural, while the Codex markers are
    plain text that a Claude transcript could quote."""
    lines = tail.splitlines()
    for name, profile in profiles.items():
        if profile.rules.get("detect_composer") and bordered_composer_index(lines) >= 0:
            # A disabled agent's screen is still that agent's: never let it
            # fall through to another agent's looser text markers.
            return name if profile.enabled else ""
    # A Claude permission prompt's footer, as the screen's last line. Text
    # quoted higher up (a README, a transcript) doesn't count.
    last = next((line.strip().lower() for line in reversed(lines) if line.strip()), "")
    # Or Claude's multiple-choice question (its question tabs and footer).
    if ("esc to cancel" in last and "tab to amend" in last) or find_choice_dialog(tail):
        for name, profile in profiles.items():
            if profile.enabled and profile.rules.get("detect_composer"):
                return name
    lowered = tail.lower()
    # Structural agents first: their text markers are specific, while the
    # Codex ones ("gpt-") are plain text another agent's transcript can quote.
    ordered = sorted(profiles.items(), key=lambda item: not item[1].rules.get("detect_composer"))
    for name, profile in ordered:
        if profile.enabled and any(marker in lowered for marker in profile.detect_contains):
            return name
    return ""


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
    # Braille spinner glyphs (U+2800–U+28FF) churn every frame; like digits,
    # they must not make one stop read as a new episode on every poll.
    normalized = "".join("#" if c.isdigit() or 0x2800 <= ord(c) <= 0x28FF else c for c in relevant)
    return hashlib.sha1(f"{state}\n{marker}\n{normalized}".encode()).hexdigest()[:12]


@dataclass
class Watcher:
    surface: str
    title: str = ""
    agent: str = "codex"
    workspace: str = ""
    prompt_fingerprint: str = ""   # the permission prompt currently on screen
    prompt_since: float = 0.0
    prompt_reported: bool = False  # an unanswered prompt is reported once
    prompt_attempts: int = 0
    prompt_answers: dict = field(default_factory=dict)  # fingerprint -> answer times
    was_busy: bool = False  # last poll saw a running turn
    near_misses: set = field(default_factory=set)  # prompt fingerprints already captured
    prompt_recheck_at: float = 0.0  # wake early to confirm a new prompt is quiet
    choice_since: float = 0.0  # a multiple-choice dialog has been showing since
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
    orphan_enters: int = 0  # Enter presses on a composer draft that exactly matches the resume command

    @property
    def profile(self) -> Profile:
        return PROFILES[self.agent]


@dataclass
class Config:
    interval: float
    dry_run: bool
    log_file: str
    stats_file: str
    watchers: dict = field(default_factory=dict)  # surface -> Watcher
    ignore_rules: tuple = (set(), [], True)


# Each log rolls over at this size and keeps one previous file, so a host
# never holds more than about twice this per log.
LOG_ROTATE_BYTES = 20 * 1024 * 1024


def open_private_append(path: str):
    """Open a log for appending, creating it readable only by the user:
    screen excerpts and prompt questions can carry private text."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    return os.fdopen(fd, "a")


def rotate_if_large(path: str) -> None:
    try:
        if os.path.getsize(path) >= LOG_ROTATE_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass


def log(cfg: Config, message: str) -> None:
    """Append to the log file, or print when there is none (the LaunchAgent
    sends stdout to a file that is never rotated). Never raises."""
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    if not cfg.log_file:
        print(line, flush=True)
        return
    try:
        rotate_if_large(cfg.log_file)
        with open_private_append(cfg.log_file) as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def record(cfg: Config, event: str, **fields) -> None:
    """Append one structured stats event as a JSON line; never blocks the loop."""
    if not cfg.stats_file:
        return
    watcher = cfg.watchers.get(fields.get("surface", ""))
    if watcher and "agent" not in fields:
        fields["agent"] = watcher.agent
    if watcher and watcher.workspace and "workspace" not in fields:
        fields["workspace"] = watcher.workspace
    payload = {"ts": datetime.now().isoformat(timespec="seconds"),
               "host": socket.gethostname(), "v": WATCHDOG_VERSION, "event": event, **fields}
    try:
        rotate_if_large(cfg.stats_file)
        with open_private_append(cfg.stats_file) as handle:
            handle.write(json.dumps(payload) + "\n")
    except Exception:
        pass


def notify(title: str, body: str) -> None:
    try:
        cmux("notify", "--title", title, "--body", body)
    except Exception:
        pass


def verify_resumed(surface: str, profile: Profile = None) -> tuple:
    """Check over ~16s whether a turn started; return (confirmed, post-state,
    post-tail) so failures keep their evidence. Codex can take well over a few
    seconds to show a running turn after /goal resume — a short window here
    mislabels successful resumes as unconfirmed (harmless for safety: the next
    poll sees the busy marker and re-arms, but it poisons the stats)."""
    state, tail = "", ""
    for attempt in range(4):
        time.sleep(VERIFY_WAIT)
        tail = read_tail(surface, profile.tail_lines if profile else 30)
        state, _ = classify(tail, profile)
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
        send_literal(watcher.surface, command, watcher.profile)
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
    confirmed, post_state, post_tail = verify_resumed(watcher.surface, watcher.profile)
    if confirmed:
        # Clear any stale pending from an earlier unconfirmed send, or the
        # next busy sighting would credit it again as a late confirmation.
        watcher.pending = None
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
            if now < watcher.next_action_at:
                return
            # A scheduled fast-lane retry is due inside the confirmation
            # window. The backoff schedule is authoritative here — this
            # preserves the retry spacing the old inline ladder had — while
            # the window stays the fallback for submissions with no earlier
            # retry scheduled (ambiguous sends, orphan Enters, slow lane).
        else:
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
        content = composer_content(tail, watcher.profile)
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
            if composer_content(read_tail(watcher.surface), watcher.profile) == pending["command"]:
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
            confirmed, post_state, _post_tail = verify_resumed(watcher.surface, watcher.profile)
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
        if composer_has_text(tail, watcher.profile):
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
        watcher.orphan_enters = 0
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
    profile = watcher.profile
    command = profile.continue_command if state == "capacity-stopped" else profile.goal_command
    if new_episode:
        log(cfg, f"{watcher.surface}: stop detected ({state}), evidence {marker!r}")
        # since_busy_s approximates detection lag: the last busy sighting is at
        # most one scan old while a turn runs, so a large value means detection
        # itself was delayed — the signal round-robin scheduling exists to fix.
        record(cfg, "stop_detected", surface=watcher.surface, title=watcher.title,
               state=state, marker=marker, fingerprint=fp, excerpt=tail_excerpt(tail),
               since_busy_s=round(now - watcher.last_seen_busy, 1) if watcher.last_seen_busy else None)
    if now < watcher.next_action_at:
        return
    if composer_has_text(tail, watcher.profile):
        content = composer_content(tail, watcher.profile)
        if content == command and watcher.orphan_enters < ORPHAN_ENTER_MAX:
            # The composer holds exactly the command we would send: either an
            # orphaned watchdog send from before a restart (pending state is
            # in-memory and lost) or a matching user draft. Submitting it is
            # the intended action either way and types nothing new — press
            # Enter, bounded, instead of deadlocking on the draft forever.
            watcher.orphan_enters += 1
            log(cfg, f"{watcher.surface}: composer holds exactly {command!r}; submitting with Enter (orphan attempt {watcher.orphan_enters})")
            record(cfg, "orphan_enter", surface=watcher.surface, title=watcher.title,
                   state=state, command=command, attempt=watcher.orphan_enters)
            try:
                cmux("send-key", "--surface", watcher.surface, "enter")
            except Exception as exc:
                log(cfg, f"{watcher.surface}: orphan Enter failed: {exc}")
                record(cfg, "send_error", surface=watcher.surface, command=command, error=str(exc))
                watcher.next_action_at = now + SLOW_LANE_INTERVAL
                return
            time.sleep(1.0)
            # The composer is the witness: if the command still sits there,
            # Enter did not take and the stop is still standing.
            if composer_content(read_tail(watcher.surface), watcher.profile) == command:
                if watcher.orphan_enters >= ORPHAN_ENTER_MAX:
                    log(cfg, f"{watcher.surface}: Enter never takes on this session; leaving it for a human")
                    record(cfg, "orphan_enter_gave_up", surface=watcher.surface, title=watcher.title,
                           command=command)
                    notify("watchdog: cannot resume a session", f"{watcher.surface}: Enter never takes")
                else:
                    log(cfg, f"{watcher.surface}: Enter did not take; will retry")
                watcher.next_action_at = now + SLOW_LANE_INTERVAL
                return
            # The command left the composer: it was submitted. Park a
            # late-confirmation pending exactly like an ambiguous retry — no
            # further sends until the turn appears or the window lapses.
            watcher.pending = {"command": command, "attempt": watcher.actions_on_stop,
                               "lane": "fast", "sent_at": now, "stop_since": watcher.stop_since}
            confirmed, _post_state, _post_tail = verify_resumed(watcher.surface, watcher.profile)
            if confirmed:
                elapsed = round(now - watcher.stop_since, 1) if watcher.stop_since else None
                log(cfg, f"{watcher.surface}: resumed via orphan Enter, turn is running")
                record(cfg, "resumed", surface=watcher.surface, title=watcher.title,
                       command=command, attempt=watcher.actions_on_stop, lane="fast",
                       since_stop_s=elapsed, via="orphan_enter")
                watcher.pending = None
            else:
                log(cfg, f"{watcher.surface}: command submitted; awaiting the turn (no further sends for this stop)")
                record(cfg, "submitted_awaiting_turn", surface=watcher.surface, title=watcher.title,
                       command=command, attempt=watcher.actions_on_stop, via="orphan_enter")
                watcher.next_action_at = now + SLOW_LANE_INTERVAL
            return
        log(cfg, f"{watcher.surface}: {state} but composer has unsent text; leaving it alone")
        record(cfg, "composer_blocked", surface=watcher.surface, state=state)
        # Re-check slowly: a user draft can sit for a while, and this keeps the
        # deferral log at its old 5-minute cadence under a 60s poll.
        watcher.next_action_at = now + 300
        return
    if cfg.dry_run:
        lane = "fast" if watcher.actions_on_stop < profile.fast_attempts else "slow"
        log(cfg, f"{watcher.surface}: DRY-RUN would send {command!r} ({lane} lane)")
        return

    # One recovery attempt per visit. The backoff schedule persists in
    # next_action_at and the main loop wakes at the earliest due timer, so a
    # session in deep backoff can never delay detection or recovery of the
    # others. The fast lane keeps the same 5s→60s exponential spacing the old
    # inline ladder had; after the twelfth attempt the slow lane retries every
    # 5 minutes until the storm passes.
    if watcher.actions_on_stop >= profile.fast_attempts:
        lane = "slow"
        backoff = profile.slow_lane_interval
    else:
        lane = "fast"
        backoff = min(FAST_BACKOFF_CAP, 5 * (2 ** watcher.actions_on_stop))
    watcher.actions_on_stop += 1
    outcome = send_once(cfg, watcher, command, state, marker, lane)
    if outcome in ("confirmed", "error"):
        # Confirmed: nothing to schedule. Error: the error path already set
        # its own retry timing (or parked an ambiguous pending).
        return
    if watcher.actions_on_stop >= profile.fast_attempts and not watcher.parked:
        # The fast ladder is spent without a confirmed resume: from the next
        # attempt on, ride out the storm at the slow lane's gentler cadence.
        watcher.parked = True
        backoff = profile.slow_lane_interval
        log(cfg, f"{watcher.surface}: slow lane after {watcher.actions_on_stop} quick attempts; retrying every {profile.slow_lane_interval // 60}m")
        record(cfg, "slow_lane", surface=watcher.surface, title=watcher.title, attempts=watcher.actions_on_stop)
        notify("watchdog: session in slow lane", f"{watcher.surface} keeps failing to resume")
    if outcome == "unconfirmed":
        # Never pile a second send into a slow-starting turn.
        backoff = max(backoff, CONFIRM_GRACE)
    watcher.next_action_at = now + backoff


def discover_surfaces(cfg: Config, witnesses: dict) -> None:
    """Find every agent session known to cmux and add it to the watch set.

    Enumeration is workspace -> pane surfaces -> a short screen read; the
    screen identifies the agent regardless of the tab title (see
    detect_agent). A watched surface whose screen now shows a different agent
    (the user quit Codex and started Claude in the same tab) switches
    profiles with fresh stop bookkeeping. Discovered surfaces that later
    vanish are dropped on read errors.
    """
    for cli in running_clis():
        discover_app(cfg, witnesses, cli)


UUID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")


def app_surfaces(cli: str) -> list:
    """Every (surface id, title) in one app: windows -> workspaces -> panes
    -> surfaces. list-pane-surfaces covers only the focused pane unless
    asked for each pane, so split layouts need the pane pass."""
    try:
        windows = [line.split()[1] for line in cmux("list-windows", cli=cli).splitlines()
                   if len(line.split()) > 1 and UUID_RE.fullmatch(line.split()[1])]
    except Exception:
        windows = []
    workspace_ids = []
    workspace_titles = {}
    for window in windows or [None]:
        extra = ("--window", window) if window else ()
        raw = cmux("list-workspaces", "--json", "--id-format", "both", *extra, cli=cli)
        data = json.loads(raw)
        workspaces = data if isinstance(data, list) else data.get("workspaces", [])
        workspace_ids += [w.get("id") for w in workspaces if w.get("id")]
        workspace_titles.update({w.get("id"): (w.get("title") or "") for w in workspaces if w.get("id")})
    surface_re = re.compile(r"surface:\d+\s+([0-9A-Fa-f-]{36})\s*(.*)")
    found = []
    for wid in dict.fromkeys(workspace_ids):
        try:
            panes = UUID_RE.findall(cmux("list-panes", "--workspace", wid, "--id-format", "both", cli=cli))
        except Exception:
            panes = []
        for pane in panes or [None]:
            extra = ("--pane", pane) if pane else ()
            try:
                listing = cmux("list-pane-surfaces", "--workspace", wid, *extra, "--id-format", "both", cli=cli)
            except Exception:
                continue
            found += [(sid, title, workspace_titles.get(wid, "")) for sid, title in surface_re.findall(listing)]
    return found


def recorded_agents(cli: str) -> dict:
    """surface id -> agent from cmux's own session records, newest record
    per surface. Screens alone can't name the agent while a permission
    prompt hides its composer."""
    try:
        data = json.loads(cmux("sessions", "list", "--json", "--all", cli=cli))
    except Exception:
        return {}
    newest = {}
    sessions = data.get("sessions") if isinstance(data, dict) else None
    for session in sessions if isinstance(sessions, list) else []:
        if not isinstance(session, dict):
            continue
        surface, agent = session.get("surface_id"), session.get("agent")
        if not isinstance(surface, str) or not isinstance(agent, str) or agent not in PROFILES:
            continue
        # Most records are of agents that have exited; a shell may now run
        # in that tab. Only a live session names the tab's agent.
        if not session.get("active_for_surface") or not session.get("stored_pid_exists"):
            continue
        stamp = session.get("updated_at_unix")
        stamp = stamp if isinstance(stamp, (int, float)) else 0
        if surface not in newest or stamp >= newest[surface][1]:
            newest[surface] = (agent, stamp)
    return {surface: agent for surface, (agent, _) in newest.items()}


def discover_app(cfg: Config, witnesses: dict, cli: str) -> None:
    try:
        surfaces = app_surfaces(cli)
    except Exception as exc:
        log(cfg, f"discovery: listing failed via {cli}: {exc}")
        return
    recorded = recorded_agents(cli)
    for sid, title, workspace in surfaces:
        title = title.replace("[selected]", "").strip()
        SURFACE_CLI[sid] = cli
        try:
            tail = discovery_tail(sid)
        except Exception:
            continue
        agent = detect_agent(tail, PROFILES) or (recorded.get(sid, "") if PROFILES.get(recorded.get(sid, ""), None) and PROFILES[recorded[sid]].enabled else "")
        existing = cfg.watchers.get(sid)
        if existing:
            existing.workspace = workspace or existing.workspace
            existing.title = title or existing.title
            if agent and agent != existing.agent and not screen_matches(existing.profile, tail) \
                    and not existing.pending and existing.actions_on_stop == 0:
                log(cfg, f"discovery: {sid} now runs {agent} (was {existing.agent})")
                record(cfg, "agent_changed", surface=sid, agent=agent, previous=existing.agent)
                cfg.watchers[sid] = Watcher(surface=sid, title=title, agent=agent,
                                            last_seen_busy=0.0)
            continue
        if not agent:
            continue
        if is_ignored(cfg.ignore_rules, sid, title):
            log(cfg, f"discovery: ignoring excluded session {sid} ({title})")
            continue
        cfg.watchers[sid] = Watcher(surface=sid, title=title, agent=agent, workspace=workspace,
                                    last_seen_busy=witnesses.get(sid, 0.0))
        log(cfg, f"discovery: now watching {agent} session {sid} ({title} in {workspace})")


def load_events(stats_file: str) -> list:
    """Every stats event, oldest first, including the rotated-out file."""
    events = []
    for path in (stats_file + ".1", stats_file):
        try:
            with open(path) as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            continue
    return events


def problems(stats_file: str, since_hours: float) -> int:
    """A short health report for one host: whether the watchdog is
    scanning, what it did, and every session that needs a person, named by
    agent, workspace, tab and surface. Exit status 1 when anything needs
    attention, so a fleet script can flag the host."""
    cutoff = datetime.fromtimestamp(time.time() - since_hours * 3600).isoformat(timespec="seconds")
    events = [e for e in load_events(stats_file) if e.get("ts", "") >= cutoff]
    host = socket.gethostname().split(".")[0]
    scans = [e for e in events if e.get("event") == "scan_completed"]
    issues = []
    if not scans:
        issues.append("watchdog has not completed a scan in this window")
    else:
        age = time.time() - datetime.fromisoformat(scans[-1]["ts"]).timestamp()
        if age > 600:
            issues.append(f"last scan {int(age // 60)} min ago; the watchdog may be stuck")
    def where(e):
        return (f"{e.get('agent', '?')} \"{e.get('title') or '-'}\" in \"{e.get('workspace') or '-'}\" "
                f"[{e.get('surface') or '?'}]")
    clears = {"resumed", "prompt_answered", "witnessed_busy", "became_busy", "prompt_gone"}
    describe = {
        "prompt_waiting": lambda e: f"prompt waiting on a person ({e.get('reason')}): {e.get('question')!r}",
        "prompt_answer_failed": lambda e: f"prompt still showing after answers: {e.get('question')!r}",
        "slow_lane": lambda e: "resume keeps failing (slow lane)",
        "orphan_enter_gave_up": lambda e: "a resume command sits unsent in the input box",
        "ambiguous_gave_up": lambda e: "a resume command may not have been submitted",
        "ambiguous_assumed_submitted": lambda e: "a resume was sent but never confirmed",
        "stale_stop_skipped": lambda e: f"stopped on {e.get('marker')!r} before the watchdog saw it working; resume it once by hand",
        "primed_stopped": lambda e: f"stopped on {e.get('marker')!r} before the watchdog saw it working; resume it once by hand",
    }
    # A near miss stays listed until that tab has had the same prompt
    # answered (a later version handled it); each open one is a case to fix.
    answered_keys = {(e.get("surface"), e.get("question")) for e in events if e.get("event") == "prompt_answered"}
    near_misses = [e for e in events if e.get("event") == "prompt_near_miss"
                   and (e.get("surface"), e.get("question")) not in answered_keys]
    counts = {}
    open_issues = {}
    for e in events:
        kind = e.get("event")
        counts[kind] = counts.get(kind, 0) + 1
        surface = e.get("surface")
        if not surface:
            continue
        if kind in clears:
            open_issues.pop(surface, None)
        elif kind in describe:
            if kind in ("stale_stop_skipped", "primed_stopped") and \
                    (e.get("marker") or "") in ("goal stalled", "goal paused", "pursuing goal (idle)", ""):
                continue  # a deliberate pause, not a problem
            open_issues[surface] = e
    for surface, e in sorted(open_issues.items(), key=lambda kv: kv[1].get("ts", "")):
        issues.append(f"{describe[e['event']](e)} — {where(e)}")
    for e in near_misses:
        issues.append(f"near miss ({e.get('reason')}): {e.get('question')!r} at {e.get('ts')}; "
                      f"screen {e.get('capture') or 'not saved'} — {where(e)}")
    print(f"{host}: {counts.get('resumed', 0)} resumed, {counts.get('prompt_answered', 0)} prompts answered, "
          f"{len(near_misses)} near misses, "
          f"{counts.get('stop_detected', 0)} stops seen, {len(scans)} scans in the last {since_hours:g} h")
    answered = [e for e in events if e.get("event") == "prompt_answered"]
    for e in answered[-5:]:
        print(f"  + {e.get('ts', '')[11:19]} answered {e.get('question')!r} — {where(e)}")
    for issue in issues:
        print(f"  ! {issue}")
    if not issues:
        print("  ok")
    return 1 if issues else 0


def report(stats_file: str) -> int:
    """Aggregate the JSONL stats into the numbers that matter for tuning."""
    events = load_events(stats_file)
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
    orphans = [e for e in events if e.get("event") == "orphan_enter"]
    orphan_gave_up = [e for e in events if e.get("event") == "orphan_enter_gave_up"]
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
    agents = sorted({e.get("agent", "codex") for e in stops + resumed})
    if len(agents) > 1 or agents != ["codex"]:
        print("by agent: " + ", ".join(
            f"{agent} {sum(1 for e in stops if e.get('agent', 'codex') == agent)} stops / "
            f"{sum(1 for e in resumed if e.get('agent', 'codex') == agent)} resumed" for agent in agents))
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
    print(f"composer-blocked deferrals: {len(blocked)}  orphan Enter submissions: {len(orphans)} (gave up: {len(orphan_gave_up)})  slow-lane entries: {len(slow)}  deliberate pauses left alone: {len(deliberate)}  stalls observed (never acted on): {len(stalls)}  stale stops skipped (witness rule): {len(stale)}  stale evidence skipped: {len(stale_ev)}  primed-on-stopped: {len(primed)}")

    # Detection lag (last busy sighting -> stop detected) and scan health:
    # the numbers that show whether round-robin scheduling keeps every
    # session's detection fast even while others are storming.
    lag = sorted(e["since_busy_s"] for e in stops if e.get("since_busy_s") is not None)
    if lag:
        mid = len(lag) // 2
        median = lag[mid] if len(lag) % 2 else (lag[mid - 1] + lag[mid]) / 2
        print(f"detection lag: median {median}s, max {lag[-1]}s")
    scans = [e for e in events if e.get("event") == "scan_completed"]
    if scans:
        durations = sorted(e.get("duration_s", 0.0) for e in scans)
        wakes = sorted(e.get("wake_s", 0.0) for e in scans)
        mid = len(durations) // 2
        med_d = durations[mid] if len(durations) % 2 else (durations[mid - 1] + durations[mid]) / 2
        med_w = wakes[mid] if len(wakes) % 2 else (wakes[mid - 1] + wakes[mid]) / 2
        print(f"scans: {len(scans)}  duration median {med_d}s, max {durations[-1]}s  wake median {med_w}s, min {wakes[0]}s")

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


PROMPT_OPTION_LINE = re.compile(r"^[❯›>]?\s*1\.\s+\S")
PROMPT_FILE_TOKEN = re.compile(r"(\S+\.[A-Za-z0-9]+)\?$")
# Greedy to the last " for this session", so a folder whose name contains
# that phrase can't cut the listed path short.
ALLOW_ACCESS_DIRS = re.compile(r"allow access to\s*(.+) for this session")
# A permission prompt is answered only after it has sat unchanged this long:
# a tab waiting on a person is quiet, and a prompt still drawing is not read.
PROMPT_QUIET_SECONDS = 5.0
# An unanswered prompt older than this is reported as waiting on a person.
PROMPT_WAITING_REPORT_SECONDS = 300.0
# The same prompt coming back after each answer (a write that keeps failing)
# is left for a person after this many answers in an hour.
PROMPT_MAX_ANSWERS_PER_HOUR = 3
# Screens of prompts a rule's question matched but its file check refused,
# kept so a miss can be turned into a test. Private; newest 50 kept.
NEAR_MISS_DIR = os.path.expanduser("~/.local/state/cmux-capacity-watchdog-misses")
NEAR_MISS_KEEP = 50


def save_near_miss(surface: str, tail: str) -> str:
    """Write the prompt's screen to NEAR_MISS_DIR and prune old ones."""
    try:
        os.makedirs(NEAR_MISS_DIR, mode=0o700, exist_ok=True)
        path = os.path.join(NEAR_MISS_DIR, f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{surface[:8]}.txt")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(tail)
        saved = sorted(os.listdir(NEAR_MISS_DIR))
        for old in saved[:-NEAR_MISS_KEEP]:
            os.remove(os.path.join(NEAR_MISS_DIR, old))
        return path
    except OSError:
        return ""


def find_pending_prompt(tail: str):
    """The permission prompt the screen ends on, if any: a question line,
    numbered options and an "Esc to cancel" hint at the bottom. Returns
    (question, file, header_lines, option_lines) or None."""
    lines = tail.rstrip("\n").split("\n")
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    # A live permission prompt is the bottom of the screen and replaces
    # Claude's framed composer. A prompt quoted in output (a cat of a saved
    # screen, a transcript) sits above a composer or shell prompt instead.
    if end == 0 or "esc to cancel" not in lines[end - 1].lower():
        return None
    if bordered_composer_index(lines) >= 0:
        return None
    option = -1
    for i in range(end - 1, max(-1, end - 20), -1):
        if PROMPT_OPTION_LINE.match(lines[i].strip()) and not lines[i].lstrip().startswith("⎿"):
            option = i
            break
    if option < 0:
        return None
    for i in range(option - 1, max(-1, option - 5), -1):
        question = lines[i].strip()
        if not question.endswith("?") or question.startswith("⎿"):
            continue
        start = i
        # A long file name wraps the question; a wrapped remainder does not
        # start with a capital.
        while start > 0 and i - start < 2 and not question[:1].isupper():
            prev = lines[start - 1].strip()
            if not prev or not prev.strip("─━-╌") or prev.endswith("?"):
                break
            start -= 1
            question = prev + " " + question
        match = PROMPT_FILE_TOKEN.search(question)
        file = os.path.basename(match.group(1)) if match else ""
        return question, file, lines[max(0, start - 60):start], lines[option:end]
    return None


def _unwrap(lines) -> str:
    """Rejoin lines the terminal wrapped, keeping a path broken mid-token
    whole."""
    out = ""
    for raw in lines:
        line = raw.strip().strip("│|").strip()
        if not line:
            continue
        if out and (out[-1] in "/-_." or (line.startswith("/") and not out.endswith(" "))):
            out += line
        else:
            out += (" " if out else "") + line
    return out


def _path_token(token: str) -> str:
    """A path-like word without surrounding quotes or trailing punctuation.
    Leading dots are part of the path (`../`, `.claude/`) and stay."""
    return token.strip("'\"`()").rstrip(".,;:")


def _expand_roots(roots) -> list:
    import glob
    expanded = []
    for root in roots:
        for path in glob.glob(os.path.expanduser(root)) or [os.path.expanduser(root)]:
            expanded.append(os.path.realpath(path))
    return expanded


def is_claude_memory_note(path: str, roots: list) -> bool:
    """`path` is absolute and names <root>/projects/<project>/memory/<n>.md
    for one of the memory roots, with symlinks resolved, the file's own
    included. A file that merely shares a memory note's name elsewhere
    never matches."""
    if not path.startswith("/") or not path.endswith(".md"):
        return False
    resolved = os.path.realpath(os.path.normpath(path))
    folder, name = os.path.dirname(resolved), os.path.basename(resolved)
    if not name.endswith(".md"):
        return False
    for root in roots:
        projects = os.path.join(root, "projects") + "/"
        if folder.startswith(projects):
            rest = folder[len(projects):].split("/")
            if len(rest) == 2 and rest[0] and rest[1] == "memory":
                return True
    return False


FILE_PROMPT_TITLES = ("Create file", "Edit file", "Overwrite file")
ALWAYS_ALLOW_OPTION = re.compile(
    r"\A2\. Yes, and always allow access to (.+?) (?:from this project|for this session)(?: \(shift\+tab\))?\Z")


def always_allow_answer(options, roots: list) -> str:
    """ "2" when option 2 is exactly "Yes, and always allow access to
    <folders>" and every folder is a Claude memory folder, else "". """
    lines = [line.strip().lstrip("❯").strip() for line in options]
    start = next((i for i, line in enumerate(lines) if line.startswith("2.")), -1)
    if start < 0:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("3.")), -1)
    if end < 0:
        return ""
    text = _unwrap(lines[start:end])
    match = ALWAYS_ALLOW_OPTION.match(text)
    if not match:
        return ""
    # A long folder wraps mid-name, leaving a space; paths here have none.
    folders = ["".join(folder.split()) for folder in match.group(1).split(",")]
    if not folders or not all(folder.startswith("/") and
                              is_claude_memory_note(os.path.join(folder, "x.md"), roots) for folder in folders):
        return ""
    return "2"


def drawn_file_path(header) -> str:
    """The one path Claude draws under its "Create file" / "Edit file" /
    "Overwrite file" title, above the preview. Everything else in the
    header (the preview between ╌ rules, earlier transcript) is written by
    the model and is never evidence. "" unless exactly one title is found
    after the last ─ rule with a single path line under it."""
    lines = [line.rstrip() for line in header]
    rule = max((i for i, line in enumerate(lines) if line.strip() and set(line.strip()) == {"─"}), default=-1)
    lines = lines[rule + 1:]
    titles = [i for i, line in enumerate(lines) if line.strip() in FILE_PROMPT_TITLES]
    if len(titles) != 1:
        return ""
    path_lines = []
    for line in lines[titles[0] + 1:]:
        stripped = line.strip()
        if not stripped or set(stripped) <= set("╌"):
            break
        path_lines.append(stripped)
    path = "".join(path_lines)          # a long path wraps without spaces
    if not path or len(path.split()) != 1:
        return ""
    return path


def prompt_memory_paths(file: str, header, options, cwd: str) -> list:
    """Absolute paths the prompt says it will write: the path Claude draws
    under its file-prompt title, resolved against the session's working
    directory when relative, plus the folders an "allow access to" option
    names. Empty when the drawn path is missing, names a different file, or
    is relative with no known working directory."""
    folders = []
    allow = ALLOW_ACCESS_DIRS.search(_unwrap(options))
    if allow:
        folders = [folder.strip() for folder in allow.group(1).split(",")]
        if not folders or not all(folder.startswith("/") for folder in folders):
            return []
    allowed = [os.path.join(folder, file) for folder in folders]
    drawn = _path_token(drawn_file_path(header))
    if not drawn:
        # A long preview scrolls the title and path off the screen. The
        # options Claude draws still name the target's folder (as typed
        # and resolved), and that is the only evidence left.
        return allowed
    if os.path.basename(drawn) != file:
        return []
    if drawn.startswith("/"):
        path = drawn
    elif drawn.startswith("~/"):
        path = os.path.expanduser(drawn)
    elif cwd:
        path = os.path.normpath(os.path.join(cwd, drawn))
    else:
        return []
    return [path] + allowed


# The one shell command answered: appending a single-quoted literal line to
# a memory note. No expansions, chaining or other redirection can hide in it.
MEMORY_APPEND = re.compile(r"\Aecho '([^']*)' >> (/[^\s;&|<>`$'\"]+)\Z")
# Or writing a literal block with a quoted heredoc (no expansion in the
# body). After the closing delimiter only a plain `echo` of literal words
# (Claude adds `echo ok` to confirm the write) may follow.
MEMORY_HEREDOC = re.compile(
    r"\Acat >>? (/[^\s;&|<>`$'\"]+) <<'([A-Za-z_][A-Za-z0-9_]*)'\n(.*)\n\2(?:\necho [A-Za-z0-9 .,:_-]*)?\Z", re.S)


# Either form may first name the note in a variable: `f=<path>; cat >> "$f" …`.
MEMORY_PATH_VARIABLE = re.compile(
    r"\A([A-Za-z_][A-Za-z0-9_]*)=(/[^\s;&|<>`$'\"]+); (echo '[^']*' >>|cat >>?) \"\$\1\" ")


def memory_write_target(command: str) -> str:
    """The memory-note path a shell command writes, if it is one of the two
    literal forms; "" for anything else."""
    command = command.strip()
    variable = MEMORY_PATH_VARIABLE.match(command)
    if variable:
        command = f"{variable.group(3)} {variable.group(2)} " + command[variable.end():]
    append = MEMORY_APPEND.match(command)
    if append:
        return append.group(2)
    heredoc = MEMORY_HEREDOC.match(command)
    if heredoc and ("\n" + heredoc.group(2) + "\n") not in ("\n" + heredoc.group(3) + "\n"):
        return heredoc.group(1)
    return ""


def bash_box_command(header) -> str:
    """The command Claude draws in its "Bash command" box, between the ╌
    rules under that title, after the last ─ rule. A long command scrolls
    the title off the top; then the first ╌ pair is the box. "" when not
    visible. Only a pointer: the command still has to be exactly one
    pending call."""
    lines = [line.rstrip() for line in header]
    rule = max((i for i, line in enumerate(lines) if line.strip() and set(line.strip()) == {"─"}), default=-1)
    lines = lines[rule + 1:]
    titles = [i for i, line in enumerate(lines) if line.strip().startswith("Bash command")]
    if len(titles) > 1:
        return ""
    start = titles[0] + 1 if titles else 0
    dashes = [i for i in range(start, len(lines)) if lines[i].strip() and set(lines[i].strip()) <= set("╌")]
    if len(dashes) < 2:
        return ""
    body = lines[dashes[0] + 1:dashes[1]]
    if not body or not all(line.lstrip().startswith("│") for line in body):
        return ""
    return "\n".join(line.lstrip()[1:].strip() for line in body)


def _squash(text: str) -> str:
    return "".join(text.split())


def prompt_rule_for(profile: Profile, prompt, cwd: str, pending=None, pending_any=None, pending_bash=None):
    """(rule, reason): the first rule that answers the prompt, or None and
    the reason none did, for the record. Every rule must say which files it
    covers; only "claude-memory" exists. `pending(file)` returns the target
    paths of the session's unanswered file tool calls for that name, used
    when the screen no longer shows the path."""
    question, file, header, options = prompt
    roots = _expand_roots(profile.rules.get("memory_roots", []))
    reason = "no rule matches the question"
    for rule in profile.rules.get("prompt_rules", []):
        pattern, answer = rule.get("question"), rule.get("answer")
        if not isinstance(pattern, str) or not isinstance(answer, str) or not answer:
            continue
        try:
            if not re.search(pattern, question):
                continue
        except re.error:
            continue
        if rule.get("files") != "claude-memory":
            reason = "rule does not say which files it covers"
            continue
        if not file:
            calls = pending_any() if pending_any else []
            shown = bash_box_command(header)
            if shown:
                # A shell command: the call is the pending Bash whose command
                # is exactly the one Claude shows, and it must be a plain
                # append of a literal line to a memory note.
                bash_calls = pending_bash() if pending_bash else calls
                matches = sorted({command for name, _path, command in bash_calls
                                  if name == "Bash" and _squash(command) == _squash(shown)})
                if len(matches) != 1:
                    reason = "the shown command is not exactly one pending call"
                    continue
                target = memory_write_target(matches[0])
                if target and is_claude_memory_note(target, roots):
                    return rule, ""
                reason = "the command is not a plain append to a Claude memory note"
                continue
            # No file named in the question: only the session's one pending
            # call can say what is being asked. Exactly one, a file edit.
            if len(calls) != 1:
                reason = "the pending action is ambiguous" if calls else "could not resolve the file's path"
                continue
            name, target, _command = calls[0]
            if name not in FILE_TOOLS:
                reason = f"the pending action is {name}, not a file edit"
                continue
            if target and is_claude_memory_note(target, roots):
                return rule, ""
            reason = "file is not a Claude memory note"
            continue
        paths = prompt_memory_paths(file, header, options, cwd)
        if not paths and pending:
            paths = pending(file)
            if len(set(paths)) > 1:
                # Two unanswered writes with this name: can't tell which one
                # is asking.
                return None, "several pending writes share this file name"
        if paths and all(is_claude_memory_note(path, roots) for path in paths):
            return rule, ""
        reason = "file is not a Claude memory note" if paths else "could not resolve the file's path"
    return None, reason


FILE_TOOLS = ("Write", "Edit", "MultiEdit")
# Only the end of a transcript can hold the call a prompt is asking about.
TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024


def _pending_calls(path: str) -> list:
    """(tool name, file_path or "", command or "") of each tool call in a
    transcript's tail that has no result yet."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    calls, answered = {}, set()
    for line in data.splitlines():
        if "tool_use" not in line and "tool_result" not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        message = row.get("message") if isinstance(row, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                target, command = tool_input.get("file_path"), tool_input.get("command")
                calls[block.get("id")] = (str(block.get("name")),
                                          target if isinstance(target, str) else "",
                                          command if isinstance(command, str) else "")
            elif block.get("type") == "tool_result":
                answered.add(block.get("tool_use_id"))
    return [call for key, call in calls.items() if key not in answered]


def pending_tool_calls(transcript: str) -> list:
    """Every unanswered tool call (name, file_path) in the session's
    transcript and its recently active subagents' (a subagent's call asks
    in the same tab). Claude records a call before asking permission for
    it, so this names the real action, unlike screen text."""
    if not transcript:
        return []
    files = [transcript]
    subagents = os.path.join(os.path.splitext(transcript)[0], "subagents")
    try:
        recent = time.time() - 3600
        files += [os.path.join(subagents, name) for name in os.listdir(subagents)
                  if name.endswith(".jsonl") and os.path.getmtime(os.path.join(subagents, name)) > recent]
    except OSError:
        pass
    found = []
    for path in files:
        found += _pending_calls(path)
    return found


FEED_FILE = os.path.expanduser("~/.cmuxterm/workstream.jsonl")
FEED_TAIL_BYTES = 8 * 1024 * 1024
FEED_WINDOW_SECONDS = 2 * 3600


def feed_permission_requests(session_id: str) -> list:
    """(tool name, file_path or "", command or "") of the session's recent
    permission requests, as cmux's Claude hook recorded them in the Feed log
    when Claude asked. Claude doesn't always write a call to its transcript
    before asking; the hook always sees it. The Feed marks a request
    "expired" once its own wait ends, while Claude's dialog stays up, so both
    pending and expired requests count; any other status means it was
    decided."""
    if not session_id:
        return []
    import base64
    key = base64.b64encode(session_id.encode()).decode().rstrip("=")
    try:
        with open(FEED_FILE, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - FEED_TAIL_BYTES))
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    cutoff = datetime.utcfromtimestamp(time.time() - FEED_WINDOW_SECONDS).strftime("%Y-%m-%dT%H:%M:%S")
    found = {}
    for line in data.splitlines():
        if key not in line or '"permissionRequest"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or not str(row.get("workstreamId", "")).rstrip("=").endswith(key):
            continue
        if str(row.get("createdAt", "")) < cutoff:
            continue
        request = (row.get("payload") or {}).get("permissionRequest") if isinstance(row.get("payload"), dict) else None
        if not isinstance(request, dict):
            continue
        try:
            tool_input = json.loads(request.get("toolInputJSON") or "{}")
        except ValueError:
            continue
        if not isinstance(tool_input, dict):
            continue
        status = row.get("status")
        open_request = isinstance(status, dict) and set(status) <= {"pending", "expired"}
        target, command = tool_input.get("file_path"), tool_input.get("command")
        found[str(row.get("id") or request.get("requestId"))] = open_request and (
            str(request.get("toolName")),
            os.path.expanduser(target) if isinstance(target, str) else "",
            command if isinstance(command, str) else "")
    return [call for call in found.values() if call]


def pending_write_paths(transcript: str, file: str) -> list:
    """Targets of the session's unanswered file tool calls for `file`."""
    if not file:
        return []
    return [path for name, path, _command in pending_tool_calls(transcript)
            if name in FILE_TOOLS and path and os.path.basename(path) == file]


_SESSION_INFO: dict = {}


def session_info(surface: str) -> tuple:
    """(working directory, transcript path) of the live agent session in a
    surface, from cmux's session records. Cached briefly; "" when unknown."""
    cached = _SESSION_INFO.get(surface)
    if cached and time.time() - cached[1] < 60:
        return cached[0]
    info = ("", "", "")
    try:
        data = json.loads(cmux("sessions", "list", "--json", "--surface", surface,
                               cli=SURFACE_CLI.get(surface, "")))
        sessions = data.get("sessions") if isinstance(data, dict) else None
        for session in sessions if isinstance(sessions, list) else []:
            if not isinstance(session, dict) or not session.get("active_for_surface") \
                    or not session.get("stored_pid_exists"):
                continue
            # Claude draws a relative path against the directory it started
            # in, which can differ from where its shell later moved.
            cwd = session.get("launch_working_directory") or session.get("cwd") or ""
            transcript = session.get("transcript_path") or ""
            session_id = session.get("session_id") or ""
            info = (cwd if isinstance(cwd, str) else "", transcript if isinstance(transcript, str) else "",
                    session_id if isinstance(session_id, str) else "")
            if info[0] or info[1]:
                break
    except Exception:
        info = ("", "", "")
    _SESSION_INFO[surface] = (info, time.time())
    return info


def session_cwd(surface: str) -> str:
    return session_info(surface)[0]


def session_pending_calls(transcript: str, session_id: str) -> list:
    """Unanswered calls from the transcripts plus the session's recent
    permission requests from the Feed, without duplicates."""
    seen, calls = set(), []
    for call in pending_tool_calls(transcript) + feed_permission_requests(session_id):
        if call not in seen:
            seen.add(call)
            calls.append(call)
    return calls


def handle_prompt(cfg: Config, watcher: Watcher, tail: str, prompt) -> None:
    """Answer a quiet prompt a rule covers, and record every prompt either
    way, so a prompt waiting on a person shows up in the fleet report."""
    question, file, header, _options = prompt
    fp = hashlib.sha1(f"{question}\n{_unwrap(header[-6:])}".encode()).hexdigest()[:12]
    now = time.time()
    if fp != watcher.prompt_fingerprint:
        watcher.prompt_fingerprint = fp
        watcher.prompt_since = now
        watcher.prompt_reported = False
        watcher.prompt_attempts = 0
        # Look again just after the quiet period instead of a full poll
        # later, so a covered prompt is answered in seconds.
        watcher.prompt_recheck_at = now + PROMPT_QUIET_SECONDS + 1
        return
    if now - watcher.prompt_since < PROMPT_QUIET_SECONDS:
        return
    profile = watcher.profile
    cwd, transcript, session_id = session_info(watcher.surface)
    calls = session_pending_calls(transcript, session_id)
    rule, reason = prompt_rule_for(
        profile, prompt, cwd,
        pending=lambda name: [path for tool, path, _cmd in calls
                              if tool in FILE_TOOLS and path and os.path.basename(path) == name],
        pending_any=lambda: pending_tool_calls(transcript),
        # The Feed log holds the request even when the transcript doesn't.
        pending_bash=lambda: calls)
    blocked = ""
    if not rule:
        blocked = reason
    elif not profile.rules.get("answer_prompts"):
        blocked = "prompt answering is off for this agent"
    elif is_ignored(cfg.ignore_rules, watcher.surface, watcher.title):
        blocked = "session is excluded"
    mentions_memory = file.endswith(".md") or "/memory/" in _unwrap(prompt[2][-30:] + list(prompt[3]))
    if not rule and fp not in watcher.near_misses and mentions_memory:
        # A prompt about a markdown file or a memory folder that no rule
        # answered: a real non-memory file, or a question or screen shape
        # the rules don't cover yet. Keep the screen so it can become a rule.
        watcher.near_misses.add(fp)
        capture = save_near_miss(watcher.surface, tail)
        log(cfg, f"{watcher.surface}: near miss ({reason}): {question!r} screen={capture}")
        record(cfg, "prompt_near_miss", surface=watcher.surface, title=watcher.title,
               workspace=watcher.workspace, question=question[:200], file=file, reason=reason,
               capture=capture)
    if blocked:
        if not watcher.prompt_reported and now - watcher.prompt_since >= PROMPT_WAITING_REPORT_SECONDS:
            watcher.prompt_reported = True
            log(cfg, f"{watcher.surface}: prompt waiting on a person ({blocked}): {question!r}")
            record(cfg, "prompt_waiting", surface=watcher.surface, title=watcher.title,
                   workspace=watcher.workspace, question=question[:200], file=file, reason=blocked,
                   waiting_s=round(now - watcher.prompt_since, 1))
        return
    recent = [t for t in watcher.prompt_answers.get(fp, []) if now - t < 3600]
    watcher.prompt_answers[fp] = recent
    if watcher.prompt_attempts >= 2 or len(recent) >= PROMPT_MAX_ANSWERS_PER_HOUR:
        if not watcher.prompt_reported:
            watcher.prompt_reported = True
            log(cfg, f"{watcher.surface}: answered {question!r} twice and it is still showing; leaving it")
            record(cfg, "prompt_answer_failed", surface=watcher.surface, title=watcher.title,
                   workspace=watcher.workspace, question=question[:200], file=file)
        return
    if cfg.dry_run:
        log(cfg, f"{watcher.surface}: DRY-RUN would answer {rule['answer']!r} to {question!r}")
        return
    watcher.prompt_attempts += 1
    try:
        # The working-directory lookup can take a moment: answer only if
        # the very same prompt is still the last thing on screen.
        current = find_pending_prompt(read_tail(watcher.surface, profile.tail_lines))
        if not current or hashlib.sha1(f"{current[0]}\n{_unwrap(current[2][-6:])}".encode()).hexdigest()[:12] != fp:
            watcher.prompt_fingerprint = ""
            return
        # cmux refuses to type into an open question or permission dialog
        # unless told to; answering that dialog is the point here, and the
        # prompt was just re-read and matched.
        answer = rule["answer"]
        if profile.rules.get("always_allow"):
            answer = always_allow_answer(prompt[3], _expand_roots(profile.rules.get("memory_roots", []))) or answer
        cmux("send", "--force", "--surface", watcher.surface, answer)
        time.sleep(1.5)
        after = find_pending_prompt(read_tail(watcher.surface, profile.tail_lines))
    except Exception as exc:
        log(cfg, f"{watcher.surface}: prompt answer failed: {exc}")
        return
    watcher.prompt_answers.setdefault(fp, []).append(now)
    if after and after[0] == question:
        return  # still showing: one more try on the next poll
    log(cfg, f"{watcher.surface}: answered {answer!r} to {question!r} ({rule.get('note', '')})")
    record(cfg, "prompt_answered", surface=watcher.surface, title=watcher.title,
           workspace=watcher.workspace, question=question[:200], file=file,
           answer=answer, waited_s=round(now - watcher.prompt_since, 1))
    watcher.prompt_fingerprint = ""


CHOICE_OPTION = re.compile(r"^ {0,3}(?:❯\s*)?(\d+)\.\s+(\[[ ✔]\]\s+)?(.*?)\s*$")
CHOICE_TABS = re.compile(r"^\s*(?:←\s+)?[☐☒]\s")
RECOMMENDED = "(Recommended)"


def find_choice_dialog(tail: str):
    """Claude's AskUserQuestion dialog at the bottom of the screen, or None.
    Two shapes: a question ("Enter to select … Esc to cancel"), and the
    review screen a multi-question dialog ends on ("Ready to submit your
    answers?" / "1. Submit answers" / "2. Cancel")."""
    lines = tail.rstrip("\n").split("\n")
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    if end == 0 or bordered_composer_index(lines[:end]) >= 0:
        return None
    tab = -1
    for i in range(end - 1, max(-1, end - 120), -1):
        if CHOICE_TABS.match(lines[i]):
            tab = i
            break
    if tab < 0:
        return None
    tabs = lines[tab].strip()
    last = lines[end - 1].strip()
    if "enter to select" in last.lower() and "esc to cancel" in last.lower():
        options, question = [], []
        for line in lines[tab + 1:end - 1]:
            match = CHOICE_OPTION.match(line)
            if match:
                options.append((int(match.group(1)), match.group(3), bool(match.group(2)),
                                "✔" in (match.group(2) or "")))
            elif not options and line.strip() and not is_rule(line):
                question.append(line.strip().strip("│").strip())
        if not options or [n for n, *_ in options] != list(range(1, len(options) + 1)):
            return None
        return {"kind": "question", "tabs": tabs, "question": " ".join(question),
                "options": options, "multi_question": "Submit" in tabs}
    filled = [i for i in range(tab + 1, end) if lines[i].strip()]
    if len(filled) >= 3 and [lines[i].strip().lstrip("❯ ") for i in filled[-3:]] == \
            ["Ready to submit your answers?", "1. Submit answers", "2. Cancel"] and "Submit" in tabs:
        body = [line.strip() for line in lines[tab + 1:filled[-3]]]
        asked = [line for line in body if line.startswith("●")]
        answers = [line[1:].strip() for line in body if line.startswith("→")]
        return {"kind": "submit", "tabs": tabs, "question": "Ready to submit your answers?",
                "asked": asked, "answers": answers}
    return None


def choice_keys(choice) -> tuple:
    """(keys to press, "") for the Recommended answer, or ((), reason)."""
    if choice["kind"] == "submit":
        answers = choice["answers"]
        if not answers or len(answers) != len(choice["asked"]):
            return (), "not every question is answered"
        if not all(answer.endswith(RECOMMENDED) for answer in answers):
            return (), "an answer other than the Recommended one was chosen"
        return ("1",), ""
    options = choice["options"]
    recommended = [(n, checked) for n, label, _box, checked in options if label.endswith(RECOMMENDED)]
    if not recommended:
        return (), "no option is marked Recommended"
    if any(box for _n, _label, box, _checked in options):
        # Multi-select: tick each Recommended option, then Tab to the next
        # question. Only inside a multi-question dialog, whose Submit tab
        # reviews the answers.
        if not choice["multi_question"]:
            return (), "a single multi-select question"
        return tuple(str(n) for n, checked in recommended if not checked) + ("<tab>",), ""
    if len(recommended) != 1:
        return (), "several options are marked Recommended"
    return (str(recommended[0][0]),), ""


def choice_fingerprint(choice) -> str:
    text = json.dumps([choice["kind"], choice["tabs"], choice["question"],
                       choice.get("options") or choice.get("answers")], ensure_ascii=False)
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def handle_choice(cfg: Config, watcher: Watcher, tail: str, choice) -> None:
    """Pick Claude's Recommended option in a multiple-choice question once it
    has waited `recommended_delay` seconds, and submit a multi-question
    dialog whose every answer is the Recommended one."""
    profile = watcher.profile
    question = choice["question"] or choice["tabs"]
    fp = choice_fingerprint(choice)
    now = time.time()
    if not watcher.choice_since:
        watcher.choice_since = now
    delay = profile.rules.get("recommended_delay", 120) if choice["kind"] == "question" else 0
    if fp != watcher.prompt_fingerprint:
        watcher.prompt_fingerprint = fp
        watcher.prompt_since = now
        watcher.prompt_reported = False
        watcher.prompt_attempts = 0
        watcher.prompt_recheck_at = max(now + PROMPT_QUIET_SECONDS, watcher.choice_since + delay) + 1
        return
    if now - watcher.prompt_since < PROMPT_QUIET_SECONDS:
        return
    keys, blocked = choice_keys(choice)
    if not blocked and not (profile.rules.get("answer_prompts") and profile.rules.get("answer_recommended")):
        blocked = "picking Recommended options is off for this agent"
    if not blocked and is_ignored(cfg.ignore_rules, watcher.surface, watcher.title):
        blocked = "session is excluded"
    if blocked:
        if not watcher.prompt_reported and now - watcher.prompt_since >= PROMPT_WAITING_REPORT_SECONDS:
            watcher.prompt_reported = True
            log(cfg, f"{watcher.surface}: question waiting on a person ({blocked}): {question!r}")
            record(cfg, "prompt_waiting", surface=watcher.surface, title=watcher.title,
                   workspace=watcher.workspace, question=question[:200], file="", reason=blocked,
                   waiting_s=round(now - watcher.prompt_since, 1))
        return
    if now - watcher.choice_since < delay:
        watcher.prompt_recheck_at = watcher.choice_since + delay + 1
        return
    recent = [t for t in watcher.prompt_answers.get(fp, []) if now - t < 3600]
    watcher.prompt_answers[fp] = recent
    if watcher.prompt_attempts >= 2 or len(recent) >= PROMPT_MAX_ANSWERS_PER_HOUR:
        if not watcher.prompt_reported:
            watcher.prompt_reported = True
            log(cfg, f"{watcher.surface}: picked Recommended for {question!r} twice and it is still showing; leaving it")
            record(cfg, "prompt_answer_failed", surface=watcher.surface, title=watcher.title,
                   workspace=watcher.workspace, question=question[:200], file="")
        return
    if cfg.dry_run:
        log(cfg, f"{watcher.surface}: DRY-RUN would press {keys} for {question!r}")
        return
    watcher.prompt_attempts += 1
    try:
        current = find_choice_dialog(read_tail(watcher.surface, profile.tail_lines))
        if not current or choice_fingerprint(current) != fp:
            watcher.prompt_fingerprint = ""
            return
        for key in keys:
            if key == "<tab>":
                cmux("send-key", "--force", "--surface", watcher.surface, "tab")
            else:
                cmux("send", "--force", "--surface", watcher.surface, key)
            time.sleep(0.5)
        time.sleep(1.0)
        after = find_choice_dialog(read_tail(watcher.surface, profile.tail_lines))
    except Exception as exc:
        log(cfg, f"{watcher.surface}: picking Recommended failed: {exc}")
        return
    watcher.prompt_answers.setdefault(fp, []).append(now)
    if after and choice_fingerprint(after) == fp:
        return  # unchanged: one more try on the next poll
    label = "submit" if choice["kind"] == "submit" else next(
        (l for n, l, _b, _c in choice["options"] if str(n) in keys), "")
    log(cfg, f"{watcher.surface}: picked {label!r} for {question!r}")
    record(cfg, "prompt_answered", surface=watcher.surface, title=watcher.title,
           workspace=watcher.workspace, question=question[:200], file="",
           answer=f"recommended: {label}"[:200], waited_s=round(now - watcher.choice_since, 1))
    watcher.prompt_fingerprint = ""


def poll_surface(cfg: Config, watcher: Watcher, pinned: set) -> bool:
    """One visit to one surface: classify, witness busy turns, act on a stop.

    Never retries inline — backoff lives in next_action_at and the loop wakes
    at the earliest due timer — so no session can delay the others. Returns
    True when witnessed-busy state changed and needs persisting.
    """
    try:
        tail = read_tail(watcher.surface, watcher.profile.tail_lines)
        choice = find_choice_dialog(tail) if "answer_recommended" in watcher.profile.rules else None
        if choice:
            handle_choice(cfg, watcher, tail, choice)
            return False
        watcher.choice_since = 0.0
        prompt = find_pending_prompt(tail)
        if prompt:
            handle_prompt(cfg, watcher, tail, prompt)
            return False
        if watcher.prompt_fingerprint and watcher.prompt_reported:
            # Someone answered a prompt that was reported as waiting.
            record(cfg, "prompt_gone", surface=watcher.surface, title=watcher.title)
        watcher.prompt_fingerprint = ""
        state, marker = classify(tail, watcher.profile)
        if state != "busy":
            watcher.was_busy = False
        if state == "retrying":
            # The agent is retrying on its own. That is neither a running
            # turn (a resume that only got this far did not work, so a
            # pending send stays unconfirmed) nor a stop. It is a witness
            # only when the user's own turn started it: a quota failure
            # spends a second on the spinner and minutes retrying, so the
            # poll rarely sees "busy". Retries after our own sends never
            # refresh the witness, or a long outage would loop for days
            # instead of ending at the witness window.
            if watcher.actions_on_stop == 0 and not watcher.pending:
                if watcher.last_seen_busy == 0:
                    record(cfg, "witnessed_busy", surface=watcher.surface, title=watcher.title,
                           via="retrying")
                watcher.last_seen_busy = time.time()
                return True
            return False
        if state == "busy":
            watcher.marker_seen = ""
            confirm_pending(cfg, watcher)
            # A running turn proves the last resume worked; re-arm.
            if watcher.last_seen_busy == 0:
                record(cfg, "witnessed_busy", surface=watcher.surface, title=watcher.title)
            elif not watcher.was_busy:
                # Cheap transition marker: lets --problems clear an alarm
                # once the session is working again, however it got there.
                record(cfg, "became_busy", surface=watcher.surface, title=watcher.title)
            watcher.was_busy = True
            watcher.last_seen_busy = time.time()
            watcher.last_fingerprint = ""
            watcher.actions_on_stop = 0
            watcher.parked = False
            watcher.stalled_polls = 0
            watcher.orphan_enters = 0
            return True
        if state == "idle" and watcher.pending and watcher.profile.evidence == "last_block" \
                and last_block(transcript_above_composer(tail), watcher.profile.block_glyphs):
            # Claude settled after our send with a new final message that is
            # not an error, and no prompt from the user since: the resume
            # worked, in a turn short enough to finish between polls.
            confirm_pending(cfg, watcher)
            watcher.marker_seen = ""
            watcher.last_seen_busy = time.time()
            watcher.last_fingerprint = ""
            watcher.actions_on_stop = 0
            watcher.parked = False
            return True
        if state in ("idle", "goal-stalled-candidate") and watcher.pending:
            # The stop settled without a confirmable turn (a short turn can
            # complete between polls, or the session was cleaned up): nothing
            # may suppress a future stop.
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
            return False
        watcher.stalled_polls = 0
        if not watcher.primed:
            # First sighting of a surface records its full episode state
            # without acting, so a stop we never saw begin is never mistaken
            # for a fresh one. The fingerprint must be primed too: otherwise
            # the next identical poll reads as a "new episode" and the reset
            # would wipe marker_seen, bypassing this guard entirely.
            #
            # Scope: this guard exists for sessions with no fresh
            # witnessed-busy record — the deliberate-stop case. When the
            # witness file proves this session was busy recently (e.g. the
            # watchdog was redeployed mid-storm), priming would instead
            # dead-letter a live stop forever: the primed fingerprint and
            # marker would read as stale on every later poll even though the
            # witness rule would have let act() resume it. Witness-fresh
            # surfaces fall through to act() on first sighting; the evidence
            # gate and the witness rule there still apply.
            watcher.primed = True
            witnessed_fresh = (watcher.last_seen_busy > 0
                               and time.time() - watcher.last_seen_busy <= WITNESS_FRESHNESS)
            if marker and not witnessed_fresh:
                watcher.marker_seen = marker
                watcher.last_fingerprint = fingerprint(state, marker, tail)
                log(cfg, f"{watcher.surface}: primed on an already-stopped session ({state}); observing only")
                record(cfg, "primed_stopped", surface=watcher.surface, title=watcher.title,
                       state=state, marker=marker)
                return False
        if state in ("goal-paused", "goal-paused-no-error", "capacity-stopped") and watcher.profile.rules.get("resume", True):
            act(cfg, watcher, state, tail, marker)
    except Exception as exc:
        if watcher.surface in pinned:
            log(cfg, f"{watcher.surface}: poll error: {exc}")
        else:
            # Discovered surface went away (tab closed); drop it.
            log(cfg, f"discovery: stopped watching {watcher.surface} ({exc})")
            cfg.watchers.pop(watcher.surface, None)
    return False


def next_wake(cfg: Config) -> float:
    """Seconds until the next scan: the earlier of the steady-state poll
    cadence or the earliest scheduled retry, so a session mid fast-lane is
    retried on its backoff schedule without holding the scan for the others."""
    now = time.time()
    due_in = [w.next_action_at - now for w in cfg.watchers.values() if w.next_action_at > now]
    due_in += [w.prompt_recheck_at - now for w in cfg.watchers.values() if w.prompt_recheck_at > now]
    if not due_in:
        return cfg.interval
    return max(1.0, min(cfg.interval, min(due_in)))


def classify_file(path: str) -> int:
    """Print how the current rules read a saved screen: the rule-tuning loop."""
    with open(path) as handle:
        tail = handle.read()
    agent = detect_agent(tail, PROFILES)
    if not agent:
        print("agent: (none detected)")
        return 1
    state, marker = classify(tail, PROFILES[agent])
    print(f"agent: {agent}\nstate: {state}\nmarker: {marker or '-'}\n"
          f"composer: {composer_content(tail, PROFILES[agent])!r}")
    return 0


def main() -> int:
    global WITNESS_FILE, DRY_RUN, INSTANCE_LOCK
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--surface", action="append", default=[], help="cmux surface id or ref; repeatable")
    parser.add_argument("--all", action="store_true", help="discover every agent session cmux knows about (all enabled agents)")
    parser.add_argument("--all-codex", action="store_true", help=argparse.SUPPRESS)  # pre-Claude spelling of --all
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between steady-state polls (default 60; sends are time-gated by backoff, so faster polls never mean faster sends)")
    parser.add_argument("--dry-run", action="store_true", help="classify and log, never send")
    parser.add_argument("--log", default="", help="append actions to this file")
    parser.add_argument("--stats", default=os.path.expanduser("~/.local/state/cmux-capacity-watchdog.jsonl"),
                        help="append structured stats events to this JSONL file")
    parser.add_argument("--witness-file", default=WITNESS_FILE,
                        help="witnessed-busy state (default: %(default)s); give a trial run its own")
    parser.add_argument("--rules", default=RULES_FILE, help="rules override file (default: %(default)s)")
    parser.add_argument("--report", action="store_true", help="print aggregate stats and exit")
    parser.add_argument("--problems", nargs="?", type=float, const=6.0, metavar="HOURS",
                        help="print this host's health and anything needing a person over the last HOURS (default 6), and exit")
    parser.add_argument("--check-rules", action="store_true", help="validate the rules file, print the effective rules, and exit")
    parser.add_argument("--classify", metavar="SCREEN_FILE", help="print how the rules read a saved screen (cmux read-screen output) and exit")
    args = parser.parse_args()
    if args.report:
        return report(args.stats)
    if args.problems is not None:
        return problems(args.stats, args.problems)
    loader = RulesLoader(args.rules)
    PROFILES.update(loader.load(lambda message: print(message, file=sys.stderr)))
    if args.check_rules:
        if loader.error:
            return 1
        print(json.dumps({name: profile.rules for name, profile in PROFILES.items()}, indent=2, ensure_ascii=False))
        return 0
    if args.classify:
        return classify_file(args.classify)
    discover = args.all or args.all_codex
    if not args.surface and not discover:
        parser.error("give at least one --surface or pass --all")

    WITNESS_FILE = args.witness_file
    DRY_RUN = args.dry_run
    # One watcher per stats file: two would both type into the same tabs.
    import fcntl
    os.makedirs(os.path.dirname(os.path.abspath(args.stats)), exist_ok=True)
    INSTANCE_LOCK = open(args.stats + ".lock", "w")
    try:
        fcntl.flock(INSTANCE_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"another watchdog already runs with {args.stats}; exiting", file=sys.stderr)
        return 0
    cfg = Config(interval=args.interval, dry_run=args.dry_run, log_file=args.log, stats_file=args.stats)
    witnesses = load_witnesses(lambda message: log(cfg, message))
    for surface in args.surface:
        for cli in running_clis():
            try:
                cmux("read-screen", "--surface", surface, "--lines", "1", cli=cli)
                SURFACE_CLI[surface] = cli
                break
            except Exception:
                continue
        try:
            agent = (detect_agent(discovery_tail(surface), PROFILES)
                     or recorded_agents(SURFACE_CLI.get(surface, "")).get(surface)
                     or "codex")
        except Exception:
            agent = "codex"
        cfg.watchers[surface] = Watcher(surface=surface, agent=agent, last_seen_busy=witnesses.get(surface, 0.0))
    ladders = ", ".join(f"{name}: x{p.fast_attempts} fast then every {p.slow_lane_interval}s"
                        + ("" if p.enabled else " (disabled)") for name, p in PROFILES.items())
    log(cfg, f"watchdog v{WATCHDOG_VERSION} starting: poll {cfg.interval}s, {ladders}; "
             f"backoff 5s->{FAST_BACKOFF_CAP}s, grace {CONFIRM_GRACE}s, "
             f"witness freshness {WITNESS_FRESHNESS // 3600}h, cmux {', '.join(running_clis())}, rules {args.rules}, "
             f"dry_run={cfg.dry_run}, discover={discover}")
    record(cfg, "watchdog_started", interval_s=cfg.interval,
           ladders={name: [p.fast_attempts, p.slow_lane_interval, p.enabled] for name, p in PROFILES.items()},
           witness_freshness_h=WITNESS_FRESHNESS // 3600, cmux=running_clis(),
           dry_run=cfg.dry_run, discover=discover, pinned=list(cfg.watchers))

    last_discovery = 0.0
    witnesses_dirty = False
    last_witness_save = 0.0
    pinned = set(args.surface)
    while True:
        scan_started = time.time()
        cfg.ignore_rules = load_ignore_rules()
        # Rule edits apply on the next cycle; watchers read PROFILES live.
        PROFILES.update(loader.load(lambda message: log(cfg, message)))
        if discover and time.time() - last_discovery >= DISCOVERY_INTERVAL:
            try:
                discover_surfaces(cfg, witnesses)
            except Exception as exc:
                log(cfg, f"discovery failed: {exc!r}")
            last_discovery = time.time()
        for watcher in list(cfg.watchers.values()):
            if not watcher.profile.enabled:
                continue
            witnesses_dirty = poll_surface(cfg, watcher, pinned) or witnesses_dirty
        if witnesses_dirty and time.time() - last_witness_save >= 60:
            save_witnesses(cfg.watchers)
            witnesses_dirty = False
            last_witness_save = time.time()
        wake_in = next_wake(cfg)
        # Scan telemetry: how long a full visit of every surface took and how
        # the wake scheduler responded — the data that shows whether scans
        # keep up and whether a storm actually shortens wake intervals.
        record(cfg, "scan_completed", surfaces=len(cfg.watchers),
               duration_s=round(time.time() - scan_started, 1), wake_s=round(wake_in, 1))
        time.sleep(wake_in)


if __name__ == "__main__":
    sys.exit(main())
