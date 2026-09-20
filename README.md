# cmux-capacity-watchdog

Watches Codex CLI sessions running in cmux and resumes the ones that stopped
on transient provider-capacity errors ("Selected model is at capacity",
server_is_overloaded, stream disconnects) — so a storm pauses work instead of
ending it.

State-aware resume:

- goal paused → sends `/goal resume`
- capacity-stopped without a goal → sends `continue`
- footer says "pursuing goal" but nothing is running (persistently) → `/goal resume`
- busy turns, clean finishes, user interrupts, and sessions with an unsent
  draft in the composer are never touched

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
machine, pointed at this checkout; `~/bin/cmux-capacity-watchdog.py` is a
symlink here. Update flow: `git pull && launchctl kickstart -k gui/$(id -u)/com.danielraffel.cmux-capacity-watchdog`.
