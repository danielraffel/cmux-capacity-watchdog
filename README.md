# cmux-capacity-watchdog

Keeps Claude Code and Codex sessions in [cmux](https://github.com/manaflow-ai/cmux)
moving while you're away. It runs in the background on your Mac, reads your
cmux tabs, and types for you in two situations:

| Situation | What it types | Agents |
|---|---|---|
| A turn stopped on a temporary provider error (model at capacity, overloaded, `API Error: 5xx`, a dropped or stalled response) | `continue`, or `/goal resume` for a Codex goal | Claude, Codex |
| Claude asks permission to create or edit one of its own memory notes ("Do you want to create MEMORY.md?"); off until you turn it on | `1` (Yes), or "Yes, and always allow" | Claude |
| Claude asks a multiple-choice question and marks one option "(Recommended)"; on with the line above, after 2 minutes | that option's number, then submits | Claude |

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
Create it if it doesn't exist. It's re-read on every check (every 15 seconds), so a change applies
without restarting anything.

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `true` | Watch this agent at all |
| `resume` | `true` | Type `continue` / `/goal resume` after a temporary provider error |
| `answer_prompts` | `false` | Answer the permission prompts its prompt rules cover (Claude memory notes) |
| `always_allow` | `true` | When answering, prefer "Yes, and always allow access to <memory folder>" |
| `answer_recommended` | `true` | With `answer_prompts` on, pick the "(Recommended)" option of Claude's multiple-choice questions |
| `recommended_delay` | `120` | Seconds a question waits for you before the Recommended option is picked |

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
`python3 ~/bin/cmux-capacity-watchdog.py --check-rules`. The file is checked
when it loads: an unknown agent or setting, a value of the wrong type (say
`"enabled": "false"` instead of `false`) or a pattern that doesn't compile
rejects the whole file. The previous good settings stay, and the log says
why.

To leave one tab alone, add its surface ID, or `title:<part of the tab title>`,
to `~/.config/cmux-capacity-watchdog/ignore`, one per line.

## Is it working?

```sh
python3 ~/bin/cmux-capacity-watchdog.py --problems      # this Mac, last 6 hours
./fleet-report.sh 24                                    # several Macs, last 24 hours
```

`--problems` prints what was resumed and answered, then names the sessions
that still need you. Those include:

- a prompt waiting more than 5 minutes;
- a prompt still showing after it was answered;
- a resume that keeps failing, or one left unsent;
- a session that was already stopped on an error when the watchdog started.

Each line gives the agent, workspace, tab title and full surface ID. An item
drops off once that session is working again, however it got unstuck. The
command exits 1 when something is listed. For `fleet-report.sh`, list
your Macs' ssh names in `~/.config/cmux-capacity-watchdog/fleet`, one per line
(`local` is this Mac).

Logs are in `~/.local/state/`:

- `cmux-capacity-watchdog.log` is readable;
- `cmux-capacity-watchdog.jsonl` holds one JSON event per action;
- `--report` totals the JSON events.

Each log rolls over at 20 MB and keeps one previous file. Both are readable
only by you, since they can quote what's on screen.

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

- It answers a prompt only after seeing it unchanged on two polls at least 5
  seconds apart (about 20 seconds after it appears), and it re-reads the
  screen just before typing.
- It answers the same prompt at most twice in a row and three times an hour.
  After that the prompt is left for you and reported.
- It answers only a live prompt at the bottom of the screen. A prompt quoted
  in output, or one with Claude's input box still showing, is ignored.
- The only built-in rule covers Claude's file prompts: create, overwrite or
  edit. It answers only when the path Claude draws under the prompt's title
  resolves to `<root>/projects/<project>/memory/<name>.md` under one of your
  Claude folders (`~/.claude`, or Subrouter's
  `~/.subrouter/codex/claude-proxy/*`). That rules out:
  - a file named `MEMORY.md` anywhere else;
  - a project's own `.claude` folder;
  - a symlink out of the memory folder;
  - a relative path when the session's directory is unknown.
- When the path has scrolled off and Claude lists no folder (Subrouter-pooled
  sessions offer only Yes / No), the evidence is the pending tool call
  itself. That's the `file_path` of the unanswered `Write` or `Edit` for
  that file name, from the session's transcript, one of its subagents', or
  the permission request cmux's Claude hook logged in its Feed
  (`~/.cmuxterm/workstream.jsonl`, matched by session ID, last 2 hours). If
  two pending writes share the name, the prompt is left for you.
- The same file prompts are answered for the asking session's own workflow
  scripts, `<root>/projects/<project>/<session-id>/workflows/scripts/<name>.js`,
  when the session ID in that path is the asking session's. Another
  session's script, or a `.js` anywhere else, is left for you.
- Text the model writes (a file preview, a command's description) is never
  used as evidence.
- Shell-command prompts ("Do you want to proceed?") are answered in only one
  case: the command Claude shows is exactly the session's one matching
  pending `Bash` call, and that call is a plain literal write to a memory
  note. There are two allowed forms:
  - `echo '<text>' >> <note>`;
  - `cat >>? <note> <<'EOF'` with a quoted heredoc, followed by nothing or
    by one `echo` of plain words.

  Either form may first name the note in a variable
  (`f=<note>; cat >> "$f" <<'EOF'`).

  Any other command is left for you.
- When Claude offers "Yes, and always allow access to <folder>" and every
  folder listed is a memory folder, it picks that option (setting
  `always_allow`, on by default), so Claude stops asking for later notes. It
  never picks an option that also switches the session to accept edits, or
  one that allows "similar commands".
- Claude's multiple-choice questions (its AskUserQuestion tool) get the
  option Claude marked "(Recommended)", once the question has waited
  `recommended_delay` seconds:
  - a multi-select question gets its Recommended options ticked;
  - a dialog of several questions is answered one question at a time, and
    its review screen is submitted only if every answer is a Recommended one;
  - a question with no Recommended option, or more than one, is left for you.

  To keep a tab's questions for yourself, list it in the `ignore` file.
- Every answer is recorded, and so is every prompt left waiting more than 5
  minutes, with the reason it wasn't answered.
- A **near miss** is a prompt whose question a rule answers but whose file
  check refused (a real non-memory file, or a screen shape the parser
  doesn't handle yet). It's recorded at once, and its screen is saved to
  `~/.local/state/cmux-capacity-watchdog-misses/` (private, newest 50 kept)
  so it can be turned into a test. `--problems` lists near misses.

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
