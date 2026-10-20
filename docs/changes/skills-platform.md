# skills-platform: skill plugins, skill proposal, Claude Code progress hook

Branch `claude/skills-platform-c373c3`. Details: [../skills.md](../skills.md), [../claude-hook.md](../claude-hook.md).

## What has been done

1. **Skill plugins.** `server/jarvis/skills/` (directory), `skills_loader.py`: discovery, static code checks,
   enabling/disabling (the `skills` table in `brain.db`), hot reload (`skills reload`, via voice using
   `skills_manage`), isolation of errors and hangs (the skill gets disabled, a message is sent to Telegram), file
   tampering protection (sha256). Sample skills: `clock` (`current_time` moved out of `brain.py`) and `dice`
   (`roll_dice`), both trusted.
   Changes to `brain.py`: loader is instantiated in `__init__`, `tools()` and `_call` pick up skills; `current_time`
   removed from `TOOLS`/`_call` (now in the `clock` skill); new optional parameter `skills_dir`.
2. **Skill proposal.** `skill_requests.py`: Gemini functions `propose_skill(title, what, why)` and
   `skill_decision(approve|reject)`; `skill_requests` table; Telegram card; Telegram commands `/skills`,
   `/approve N`, `/reject N`, `/enable name`, `/disable`, `/reload`, `/diff N` (in `telegram.py` additively: `skills`
   field and early command parsing before Gemini); the same commands in the console (`hub.skills`).
3. **Laptop runner.** `server/tools/skill_runner.py` + `skill_job` handling in `pc/agent.py`; the bridge
   (`bridge.py`) forwards `skill_progress`/`skill_result` via `on_pc_event` and reports laptop connections
   (`on_pc_connect`: queued jobs get dispatched). Security model: `docs/skills.md`.
4. **Progress hook.** `server/tools/claude_hook.py` (Claude Code) → `/admin` service endpoint → `claude_status.py` →
   `task_status`. States `working` (percentage based on todos), `needs_permission`, `needs_input`, `done`; completion
   announcement controlled by the `claude speak on|off` setting. Added `task_status(state, label, pct)` to
   `protocol.py` (format matching protocol-next.md). **When merging with display:** if that function already exists
   there, keep only one; mine truncates the label strictly to 31 bytes (`_fit`), whereas the standard `_clip` can
   produce up to 34.

## New `.env` variables

None. The hook reads the existing `JARVIS_SERVER`, `JARVIS_PORT`, `JARVIS_DEVICE_TOKEN`.

## New console commands (and `tools/send.py`)

`skills [list]`, `skills enable|disable <name>`, `skills reload [name]`, `approve N`, `reject N`, `enable name`,
`diff N`, `claude` (sessions), `claude speak on|off`, `claude_event <json>` (service command, from the hook).

## Changes to shared files (all additive)

`brain.py` (see above), `telegram.py` (+8 lines), `bridge.py` (+9: two callbacks and a `skill_*` branch in `on_pc`),
`console.py` (two modules in command dispatch), `devices.py` (`skills`, `claude` fields), `__main__.py` (+14: wiring),
`protocol.py` (`task_status`), `pc/agent.py` (+27: `skill_job`). Untouched: version, CHANGELOG, roadmap, ideas.

## Tests

Total `262 passed, 2 skipped` (was 195). New tests: `test_skills.py` (28: loader, code checks, lifecycle,
failures, timeout, file tampering, ctx), `test_skill_requests.py` (12: proposal, duplicates, limit, approval,
offline laptop, voice «да» ("yes"), the full path to `/enable`, Telegram), `test_skill_runner.py` (14: `claude`
command, allowlist, extra files, code checks, tests, timeout, real git in a temp repo),
`test_claude_status.py` (13: states, session priority, TTS announcement, hook over real WebSocket).

## NOT tested live

- Real invocation of `claude -p` with these flags (`--permission-mode dontAsk`, `--permission-prompts none`,
  `--allowedTools "Edit(path)"`, `--tools`): flags were verified against current documentation, but never actually run
  (requires Claude Code login and funds). The first production run should be done manually: `skill_runner.py job.json`.
  If the `Bash(python -m pytest …)` rule proves too strict, Claude won't be able to run tests, but the runner will
  still run them itself and report honestly.
- Real Claude Code hooks and the `tool_input` shape of `TodoWrite` (`todos[].status`); rendering of `task_status` on the speaker.
- Telegram cards and voice «да, делай» ("yes, do it") against live services; the pipeline through the real `/pc`
  (only the bridge handler and `skill_runner` were tested separately).
- HA deployment was not performed, the speaker was untouched.

## Remaining work / ideas

- Skills run inside the server process; truly untrusted skills require a separate process.
- No "public" Gemini hints on when to call `propose_skill` (the function description in `TOOLS` is sufficient,
  but a line should be added to the system prompt `voice.system_prompt` after merging).
- Console help strings (`help`) were not updated (shared file).
- External prerequisites: none. A real production run of the runner on the laptop requires `claude` to be installed and authenticated.
