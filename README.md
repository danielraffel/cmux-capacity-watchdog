# cmux-capacity-watchdog

Keeps Claude Code and Codex sessions in [cmux](https://github.com/manaflow-ai/cmux)
moving while you're away. It runs in the background on your Mac, reads your
cmux tabs, and types for you in two situations:

| Situation | What it types | Agents |
|---|---|---|
| A turn stopped on a temporary provider error (model at capacity, overloaded, `API Error: 5xx`, a dropped or stalled response) | `continue`, or `/goal resume` for a Codex goal | Claude, Codex |
| Claude asks permission to save one of its own memory notes ("Do you want to create MEMORY.md?") | `1` (Yes) | Claude |

Everything else (other permission prompts, a deliberate pause, a finished
turn, an unsent draft) is left for you. It works with or without
[Subrouter](https://github.com/manaflow-ai/subrouter) and needs nothing from it.

## Install

```sh
git clone https://github.com/danielraffel/cmux-capacity-watchdog.git
cd cmux-capacity-watchdog
./install.sh
```

That's all. It installs a LaunchAgent that starts at login and restarts if it
exits; nothing runs in a terminal. To update later: `git pull && ./install.sh`.
`./install.sh --status` shows whether it's running; `./install.sh --uninstall`
removes it.

It watches every Claude and Codex tab in every running cmux app (release and
nightly). If a cmux app only accepts automation from processes it started
(its default), set **Settings → Automation → Socket control** to *Password* or
*Allow all* so a background process can read its tabs.

## Settings

Out of the box: **resume is on for Claude and Codex; answering prompts is off.**

Change that in one file, `~/.config/cmux-capacity-watchdog/rules.json`.
Create it if it doesn't exist. It's re-read every minute, so a change applies
without restarting anything.

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `true` | Watch this agent at all |
| `resume` | `true` | Type `continue` / `/goal resume` after a temporary provider error |
| `answer_prompts` | `false` | Answer the permission prompts its prompt rules cover (Claude memory notes) |

Each setting is per agent: put it under `"claude"` or `"codex"`.

**Claude only** (leave Codex alone):

```json
{ "agents": { "codex": { "enabled": false } } }
```

**Codex only:**

```json
{ "agents": { "claude": { "enabled": false } } }
```

**Resume both, and answer Claude's memory-note prompts:**

```json
{ "agents": { "claude": { "answer_prompts": true } } }
```

**Answer Claude's prompts but never type `continue` for it:**

```json
{ "agents": { "claude": { "resume": false, "answer_prompts": true } } }
```

Check what's in force with
`python3 ~/bin/cmux-capacity-watchdog.py --check-rules`. A file with a mistake
is ignored (the previous good settings stay) and the log says why.

To leave one tab alone, add its surface ID, or `title:<part of the tab title>`,
to `~/.config/cmux-capacity-watchdog/ignore`, one per line.

## Is it working?

```sh
python3 ~/bin/cmux-capacity-watchdog.py --problems      # this Mac, last 6 hours
./fleet-report.sh 24                                    # several Macs, last 24 hours
```

`--problems` prints what was resumed and answered, then names every session
that needs you: a prompt nobody answered, a resume that keeps failing, a
session that was already stopped when the watchdog started. Each line gives
the agent, workspace, tab title and surface ID. For `fleet-report.sh`, list
your Macs' ssh names in `~/.config/cmux-capacity-watchdog/fleet`, one per line
(`local` is this Mac).

Logs are in `~/.local/state/`:

- `cmux-capacity-watchdog.log` is readable;
- `cmux-capacity-watchdog.jsonl` holds one JSON event per action;
- `--report` totals the JSON events.

Each log rolls over at 20 MB and keeps one previous file.

## How it decides

**Resume.**

- It acts only when the error is on screen *and* it saw that session working
  in the last 8 hours. A tab that was already stopped when it first saw it is
  left alone, so abandoned tabs stay abandoned. Resume such a tab once by hand
  and it's covered from then on.
- For Claude, the error must be Claude's last message: an old error followed
  by a normal reply doesn't count.
- It never types over a draft and never interrupts a running turn.
- Retries back off:
  - Codex: 12 quick attempts, then every 5 minutes.
  - Claude: 3 quick attempts (Claude already retries 10 times itself), then
    every 15 minutes.

**Prompts.**

- It answers only a prompt that has sat unchanged for 5 seconds, at most
  twice. If it's still showing, it's left for you and reported.
- A memory-note prompt is answered only when the file resolves to
  `<root>/projects/<project>/memory/<name>.md` under one of your Claude
  folders (`~/.claude`, or Subrouter's `~/.subrouter/codex/claude-proxy/*`).
  A file named `MEMORY.md` anywhere else, or a project's own `.claude`
  folder, never qualifies.
- Every prompt is recorded, answered or not.

## Advanced rules

All behavior lives in `DEFAULT_PROFILES` near the top of
`cmux-capacity-watchdog.py`: which errors count, retry timing, the prompt
rules, the memory folders. Any of it can be overridden in `rules.json`. A key
replaces the default, and a `<key>_add` key appends to a list:

```json
{
  "agents": {
    "codex":  { "slow_lane_interval": 600 },
    "claude": { "evidence_regex_add": ["^[⏺●] API Error: 429\\b"],
                "memory_roots_add": ["~/work/claude-config"] }
  }
}
```

Grow these from what actually happened (the JSON log records the on-screen
text of every stop), not speculatively. To try a rule against a real screen:

```sh
cmux read-screen --surface <id> --lines 80 > screen.txt
python3 cmux-capacity-watchdog.py --classify screen.txt
```

## Development

```sh
python3 test_cmux_capacity_watchdog.py
```

`fixtures/` holds real Claude Code screens captured against
`e2e/anthropic_stub.py`, a local stand-in for the Anthropic API that can
return Subrouter's "no non-exhausted accounts" 503, a slow success, or a
`Write` tool call. Point a throwaway Claude at it
(`ANTHROPIC_BASE_URL=http://127.0.0.1:31499`, its own `CLAUDE_CONFIG_DIR`) to
rehearse a resume or a prompt end to end without spending tokens.
