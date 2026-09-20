# cmux-capacity-watchdog

Watches Codex CLI sessions running in cmux and resumes the ones that stopped
on transient provider-capacity errors ("Selected model is at capacity",
server_is_overloaded, stream disconnects) — so a storm pauses work instead of
ending it.

State-aware resume, evidence-gated:

- goal paused **with a transient error on screen** → sends `/goal resume`
- capacity-stopped without a goal → sends `continue`
- every action requires a known transient-error signature (capacity, overload,
  stream disconnect, 503). A session that stopped for any other reason — user
  pause, Esc interrupt, clean finish, deliberate abandonment — is never
  touched. A paused goal with no error visible is counted
  (`deliberate_pause_skipped`) and left alone.
- a "pursuing goal" footer with no running turn is logged as `stall_observed`
  but never acted on: no error signature, no resume.
- busy turns and sessions with an unsent draft in the composer are never touched

Explicit exclusions live in `~/.config/cmux-capacity-watchdog/ignore` (one
surface UUID or `title:<substring>` per line, reloaded every cycle — edits take
effect immediately, no restart).

Runs gently: three quick attempts per stop (30s/60s/120s apart), then a slow
lane of one retry every 15 minutes until the storm passes, with a cmux
notification when it enters the slow lane.

## Run

```
python3 cmux-capacity-watchdog.py --all-codex            # watch every Codex session cmux knows
python3 cmux-capacity-watchdog.py --surface <id> [...]   # or pin specific surfaces
python3 cmux-capacity-watchdog.py --report               # aggregate stats and exit
```

Discovery re-scans every 5 minutes; new Codex sessions are picked up
automatically and closed tabs dropped.

## Stats

Every stop and action lands in `~/.local/state/cmux-capacity-watchdog.jsonl`
(host-tagged): the matched error marker, a short excerpt of the actual on-screen
error, which command was sent, which lane (fast/slow), whether the resume was
confirmed, and the post-attempt screen state when it wasn't. `--report`
aggregates: stops by type and marker, resume success/attempts/latency, flap
rate (new stop within 5m of a resume), lane split, and per-surface offenders.
The point is answering "what do we tune next" from data, not vibes.

## Deploy

Runs as a LaunchAgent (`com.danielraffel.cmux-capacity-watchdog`) on each
machine. `~/bin/cmux-capacity-watchdog.py` is a plain copy of the script —
not a symlink; launchd/TCC refuses to exec a symlink whose target lives on an
external volume. Update flow: `git pull` on both machines, `cp` the script to
`~/bin/`, then `launchctl kickstart -k gui/$(id -u)/com.danielraffel.cmux-capacity-watchdog`.

## Known issues

Tracked in GitHub issues. Notably
[#1](https://github.com/danielraffel/cmux-capacity-watchdog/issues/1):
deliberately stopped or abandoned sessions can be auto-resumed today; see the
issue for candidate mitigations (witness-only resumes, freshness windows,
per-surface caps) and the stats signals that will tell us how real the problem
is before we pick one.
