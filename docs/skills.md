=== File: skills.md ===
# Maga's Skills: Plugins She Offers and Writes Herself

Idea: Maga notices she doesn't know how to do something (or you tell her "learn to do X"), proposes a skill, you agree,
Claude Code on the laptop writes the skill and tests in a separate branch, you review the diff, and only then enable it.
Nothing is merged or deployed automatically.

## What It Consists Of

| Part | Where | What it does |
|---|---|---|
| Skill | `server/jarvis/skills/<name>.py` | module with `META`, `TOOLS`, `async def call(name, args, ctx)` |
| Loader | `server/jarvis/skills_loader.py` | finds, checks, enables, disables, reloads, isolates errors |
| Proposals | `server/jarvis/skill_requests.py` | `propose_skill`, card in Telegram, `/approve`, `/reject`, `/enable`, job for the laptop |
| Runner | `server/tools/skill_runner.py` (+ `pc/agent.py`) | on the laptop: worktree, `claude -p`, checks, tests, commit in branch |

In `brain.py` the hook-in is minimal: `Brain.skills` (the loader) adds its functions to `tools()`, and `Brain._call`
first asks the loader. Samples: `skills/clock.py` (`current_time`, moved from `brain.py`) and
`skills/dice.py` (`roll_dice`).

## Skill Format

```python
META = {"name": "btc", "description": "Bitcoin price", "version": "1.0", "permissions": ["net"]}
TOOLS = [{"name": "btc_price", "description": "...", "parameters": {"type": "OBJECT", "properties": {}}}]

async def call(name, args, ctx):
    data = await ctx.http_get_json("https://api.example.com/price", {"cur": "eur"})
    return {"ok": True, "price": data["eur"]}
```

`META["name"]` matches the filename (Latin letters, digits, `_`). Function names must not clash with other modules' functions.
What is available in `ctx`: `now()`, `log`, `pref/set_pref` (settings specific to this skill only), and per permissions:

| Permission | What it opens |
|---|---|
| `net` | `ctx.http_get_json(url, params)`: https only, response up to 1 MB, 10 s timeout; `urllib`, `http`, etc. imports are allowed |
| `ha` | `ctx.ha` (Home Assistant object) |
| `google` | `ctx.google` |
| `files` | `ctx.read_text/write_text` only inside `data/skills/<name>/`, file up to 200 KB |

Without the respective permission, the field in `ctx` is missing (`None`) or the method raises a `PermissionError`.

## Lifecycle

1. **Proposal.** Gemini calls `propose_skill(title, what, why)`. A record is saved in `brain.db` (`skill_requests`, status
   `proposed`), a card in Telegram: "I propose a skill: … Do you agree? /approve N or /reject N". No more than 5
   unresolved proposals; a repeat of the same name does not create a new card.
2. **Decision.** `/approve N` or `/reject N` in Telegram, or by voice «да, делай» ("yes, do it" — Gemini calls
   `skill_decision`; the number is omitted if there is only one pending proposal). `/skills` shows skills and proposals.
3. **Execution.** Approval sends a `skill_job` to the laptop via the PC bridge (`/pc`). If the laptop is offline,
   the job waits and is dispatched as soon as `pc/agent.py` connects.
4. **Result.** `skill_result`: branch `skill/<name>`, list of files, diff, test results. Telegram receives
   "skill is ready: view diff" (`/diff N` shows the full diff, up to 6000 characters).
5. **Enabling.** You do it yourself: `git diff main...skill/<name>`, merge the branch, deploy the server, then `/enable <name>`.
   Until the file exists on the server, `/enable` honestly replies that the skill is not in the folder. By voice, enabling a skill can only be done
   via a direct request «включи навык …» ("enable skill ..." — function `skills_manage`).

Skill management (Telegram and console): `skills` (list), `skills enable|disable <name>`, `skills reload [name]`,
`/diff N`. By voice: «перезагрузи навыки» ("reload skills"), «выключи навык …» ("disable skill ...") (function `skills_manage`).

## Security Model

Main principle: skill code enters the server only via a branch reviewed by a human, and is enabled only
upon an explicit "yes". Everything else is layers that mitigate harm from bugs or malicious payloads.

**What a skill can do.** Respond to Gemini calls; read time; make https requests where permitted by `net`
(the server does not filter addresses, check the diff: where exactly the skill connects); read and write its own folder when given `files`;
talk to Home Assistant and Google when given `ha`/`google` (with the same permissions as Maga herself: therefore, read such
skills most carefully).

**What a skill cannot do (checked before execution).** `check_source` parses the code and rejects: imports of `os`, `sys`,
`subprocess`, `socket`, `ctypes`, `shutil`, `importlib`, `pickle`, `threading`, `inspect`, `types` and other
dangerous modules; network modules without `net`; `pathlib`/`io` without `files`; `open()`, `eval`, `exec`, `compile`,
`__import__`, `getattr/setattr`, access to `__class__`, `__globals__`, `__subclasses__` and similar; import of `jarvis`
(everything needed is in `ctx`). If violated, the skill fails to load, and you get a message.

**Honest limitation.** This is not a sandbox. The skill runs inside the server process, and static checks catch the obvious,
yet do not protect against sophisticated bypasses. Therefore, the main defense is human diff review and manual enabling.
For truly untrusted code, a separate process is needed (planned for later if the number of skills grows).

**Error isolation.** An exception or a freeze (20 s) in `call` does not crash the conversation: the skill is disabled, Telegram receives
a message "skill … disabled: reason", Gemini gets `{"ok": false, "error": …}`. `skills reload <name>` tries
again after a fix.

**File substitution.** Upon enabling, the file's sha256 hash is written to the database. If the file has changed and you haven't re-enabled it, upon
server startup it will not load (except for trusted `clock` and `dice` located in the repository from the start). Explicit
`skills reload` accepts the new version.

**Rollback.** `skills disable <name>` (instantly, without restart), delete the file and run `skills reload`, or
`git revert` of the merge and a new deployment. Enablement state resides in `brain.db` (table `skills`).

**Runner on the laptop** (`tools/skill_runner.py`):
- works in a separate git worktree `.claude/worktrees/skill-<name>` on a new branch `skill/<name>` from the current
  commit; the main branch and working directory are not touched; if a branch with this name already exists — aborts;
- Claude Code is launched as: `claude -p … --permission-mode dontAsk --permission-prompts none --tools
  Read,Glob,Grep,Edit,Write,Bash --allowedTools Read Glob Grep "Edit(<skill file>)" "Edit(<test file>)"
  "Bash(python -m pytest -q <test file>)" --max-turns 20 --max-budget-usd 1.50 --no-session-persistence`.
  No web, no subagents, any action outside the whitelist is rejected without questions. The `Edit(path)` rule
  also covers Write (Claude Code documentation, Permissions section). User rules from `~/.claude/settings.json`
  still apply: if broad permissions are configured there, they remain active;
- limits per build: 20 turns, 1.5 US dollars (client estimation), 12 minutes wall-clock time (on timeout the process is killed along with children);
  configured in `SkillRunner(...)`;
- environment without secrets: variables containing `TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, `REFRESH` are stripped (except
  `ANTHROPIC_*` and `CLAUDE_*`, which Claude Code itself needs);
- after startup: `git status` must show only two files (`server/jarvis/skills/<name>.py` and
  `server/tests/skills/test_<name>.py`); any other modified file results in abort without commit; then the same `check_source` check; then `pytest` on the skill test; only after that a commit is made in the branch;
- **does not** run `push`, `merge`, `checkout`, does not deploy, and does not touch the server or the speaker.
- the task text (from Gemini's words) in the prompt is marked as the skill description rather than Claude commands; even if it contains
  a trap, Claude Code's rights are restricted to two files, and a human reads the result.

Skill tests from `server/tests/skills/` reside in the skill branch and after merging are run along with all tests
(`python -m pytest -q`).

## Laptop Setup

- Claude Code is installed (`claude` in PATH) and logged in.
- `pc/agent.py` is running (it listens for `skill_job`); the repository on the laptop is at `C:\dev\jarvis` with `server/.venv`.
- The base revision is the current `HEAD` of the main repository. If you want a different branch, switch it before approving.
- Manual launch without the server: `server\.venv\Scripts\python server\tools\skill_runner.py job.json`, where
  `job.json` contains fields `id`, `name`, `title`, `what`, `why`.

## Protocol (Server ↔ Laptop)

Unknown types are ignored by the receiver, as everywhere else.

```json
{"type": "skill_job", "id": 3, "name": "btc", "title": "Bitcoin price", "what": "...", "why": "..."}
{"type": "skill_progress", "id": 3, "text": "Claude Code is writing the skill"}
{"type": "skill_result", "id": 3, "ok": true, "branch": "skill/btc", "files": ["..."], "diffstat": "...",
 "diff": "first 3000 chars", "tests": "3 passed", "cost": 0.42, "seconds": 210}
{"type": "skill_result", "id": 3, "ok": false, "error": "tests failed: 1 failed"}
```

Statuses in `skill_requests`: `proposed` → `approved` → `running` → `ready` → `enabled`; also `rejected`, `failed`.


## Build Limits (to avoid burning through Claude subscription)

Each skill build invokes `claude -p` and consumes subscription limits. Therefore, the server itself limits builds:
by default **one build per day, three per week, a 4-hour pause between builds**. Approved builds exceeding the limit do not
disappear: they wait, and Maga writes when they can proceed (`/approve N` later). Settings in `.env`:
`SKILL_MAX_PER_DAY`, `SKILL_MAX_PER_WEEK`, `SKILL_MIN_GAP_H`. Kill switch: `/skills build off`
(`/skills build on` turns it back on, `/skills build` shows counters). The build itself still only runs after
your "yes": Maga cannot trigger it on her own.
