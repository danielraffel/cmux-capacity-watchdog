# cmux-capacity-watchdog

Keeps Codex and Claude Code sessions in cmux going when the provider has a
bad moment. When a session stops on a transient error (Codex "Selected model
is at capacity", a Subrouter `503 no non-exhausted claude accounts
available`, a dropped stream), the watchdog types the resume command into
that tab once the stop is confirmed, and keeps retrying gently until the
session runs again.

It runs on its own as a LaunchAgent, alongside Subrouter or without it. It
reads tabs through the cmux CLI and needs nothing from Subrouter, so a
Subrouter update can't break it. Nothing has to be launched by hand after a
reboot.

| Agent | Resumes on | Types |
|---|---|---|
| Codex | goal paused with a capacity/overload/stream error on screen | `/goal resume` |
| Codex | turn stopped with one of those errors, no goal | `continue` |
| Claude Code | its last message is `API Error: 5xx`, overloaded, a dropped or stalled response, or a timeout | `continue` |

## Install (each Mac, once)

```sh
git clone git@github.com:danielraffel/cmux-capacity-watchdog.git
cd cmux-capacity-watchdog
./install.sh
```

`install.sh` copies the script to `~/bin/`, writes the LaunchAgent
(`com.danielraffel.cmux-capacity-watchdog`, starts at login, restarted if it
exits) and starts it. To update: `git pull && ./install.sh`. Also:
`./install.sh --status` and `./install.sh --uninstall`.

It watches every Codex and Claude tab in every running cmux app (release and
nightly side by side), and picks up new tabs within 5 minutes.

## When it acts, and when it never does

Every resume needs evidence: a known transient-error signature from the
current stop. A session that stopped any other way is never touched: user
pause, Esc interrupt, a clean finish, a deliberately abandoned tab.

- **Busy turns** are never touched.
- **A paused goal with no error visible** is a deliberate pause. It is left
  alone (`deliberate_pause_skipped`).
- **A draft in the composer** blocks action. One exception: a draft that
  exactly matches the resume command is submitted with Enter, at most 3
  times. It is either the watchdog's own send from before a restart or the
  same thing the user meant to type.
- **Witness rule.** A stop can only be resumed if the watchdog saw that
  session working within the last 8 hours (persisted across restarts). A tab
  found already stopped is logged as `stale_stop_skipped` and left alone. So
  a session that dies while the watchdog is down stays stopped until you
  resume it once by hand.
- **Codex** evidence must be in the last 12 lines of the screen. An idle
  "pursuing goal" footer with no error is logged (`stall_observed`) and never
  acted on.
- **Claude** evidence must be Claude's last message above the composer. An
  old error followed by a normal reply, or by a newer prompt from you, does
  not count. Claude's own `Retrying in Ns · attempt k/10` loop counts as
  working, never as a resume: a `continue` is confirmed only by a real
  running turn. Those retries count toward the witness rule only when your
  own turn started them, so a long quota outage still ends at the 8-hour
  window.

Exclusions: `~/.config/cmux-capacity-watchdog/ignore`, one surface UUID or
`title:<substring>` per line. It is re-read every cycle and takes effect
immediately.

## Retry timing

Polls are cheap (every 60 seconds) and only detect. Sends are spaced by
backoff, confirmation windows and evidence novelty, so polling faster never
means sending faster. Recovery is round-robin: each visit makes at most one
attempt per session, so a session storming through retries never delays the
others.

| | Fast attempts | Spacing | Then |
|---|---|---|---|
| Codex | 12 | 5s doubling to 60s | every 5 minutes |
| Claude | 3 | 5s doubling, plus Claude's own 10 retries in between | every 15 minutes |

After an unconfirmed send, nothing else is sent for at least 45 seconds, so a
turn that is slow to start never gets a duplicate. Every visit re-reads the
screen first: typing, a state change, or the session resuming on its own
means no attempt happens.

## Rules

All per-agent behavior lives in `DEFAULT_PROFILES` in the script: how the
screen is recognised, busy markers, composer shape, evidence, commands and
retry timing. To change it on one Mac without editing code, create
`~/.config/cmux-capacity-watchdog/rules.json`:

```json
{
  "agents": {
    "claude": {"evidence_regex_add": ["^[⏺●] API Error: 429\\b"]},
    "codex":  {"slow_lane_interval": 600}
  }
}
```

- A key replaces the default. A `<key>_add` key appends to a list.
- `"enabled": false` turns an agent off.
- The file is re-read every cycle. No restart of the watchdog or of any
  session is needed.
- A malformed edit keeps the last good rules and says so in the log.
- `python3 ~/bin/cmux-capacity-watchdog.py --check-rules` validates the file
  and prints the effective rules.

Grow evidence from what stats actually captured, never speculatively. To try
a rule against a real screen, save it with
`cmux read-screen --surface <id> --lines 80 > screen.txt` and run
`python3 cmux-capacity-watchdog.py --classify screen.txt`. Rules that prove
out belong in `DEFAULT_PROFILES` plus a fixture in `fixtures/`, so every Mac
gets them on the next `git pull && ./install.sh`.

## Stats

Every stop and action lands in `~/.local/state/cmux-capacity-watchdog.jsonl`,
tagged with host, agent and behavior version (`WATCHDOG_VERSION`, bumped on
every behavior change). `python3 ~/bin/cmux-capacity-watchdog.py --report`
aggregates them, segmented by version:

- stops by type and matched marker, with an example excerpt
- resume success, attempts and latency
- the flap rate (a new stop within 5 minutes of a resume)
- lane split and the worst-offending sessions

The human-readable log is `~/.local/state/cmux-capacity-watchdog.log`.

## Tests

```sh
python3 test_cmux_capacity_watchdog.py
```

The unit tests cover the scheduling guarantees, the rules file, and Claude
screen handling. `fixtures/` holds real Claude Code screens captured against
`e2e/anthropic_stub.py`, a local stand-in for the Messages API that can
return Subrouter's exact 503 or a slow success. Run a throwaway Claude against
it (`ANTHROPIC_BASE_URL=http://127.0.0.1:31499`) to capture new screens or
rehearse a resume end to end, with no tokens spent.

## Known issues

Tracked in GitHub issues, notably
[#1](https://github.com/danielraffel/cmux-capacity-watchdog/issues/1)
(deliberately stopped sessions and auto-resume).
