# hunches: "guesses" (#45), Maga notices habits and suggests automations

New module `server/jarvis/hunches.py`, tests `server/tests/test_hunches.py` (22 tests using HA, time, and REST mocks).
The file `docs/market-research.md` is not in the repository, so I relied on the task description.

## What was done
- **Event log** (`events` table in `brain.db`, 60 days): lights and lamps from HA (dedicated WebSocket connection,
  `subscribe_events state_changed`, without polling; auto-reconnecting), "good night" («спокойной ночи»), music playback,
  alarm setting, and start/end of PC work (polling `bridge.pc_active` once a minute; a state that lasts for 5 minutes
  becomes the event).
- **Human actions only**: events with `context.parent_id` (automations, scenes) are discarded; Maga's own calls
  (`ha.request` is wrapped, remembering the `context.id` of the `call_service` response), direct WLED lamp commands,
  and "good night" («спокойной ночи») are also excluded. User commands via Gemini tools (`ha.call`) are considered user actions.
  Lamp groups are skipped (individual members arrive on their own). The day starts at 4 AM ("00:10" belongs to the previous evening).
- **Pattern search** once a day after 03:40: a single action at the same time ±15 minutes in at least 5 of the last
  7 days ("every evening") or 4 of the last 5 weekdays ("on weekdays"). Lamps of the same moment (±10 min) are merged
  into a single hunch. Candidates: `learned(kind=hunch, status=candidate)`, structure in the `hunches` table.
  We can only automate lighting (on/off). Other patterns (PC, alarm, music, "good night" («спокойной ночи»)) are saved
  as `status=insight` and are visible in `hunch list`, but are not suggested.
- **Suggestion**: via voice (`Voice.announce`), or via Telegram if the speaker is busy or missing. Conditions: not at night,
  from 11 AM to 9 PM, not during "don't listen" («не слушай»), only when `presence.here()`, no more than once a day,
  not while an unanswered suggestion is pending, and while active Maga automations are fewer than 3. An unanswered suggestion
  returns to candidates after 36 hours, and after two silences, it is considered a "no".
- **"Yes"** creates an automation via a REST request `POST {api}/config/automation/config/maga_hunch_<id>_<ts>`
  (there is no WebSocket command to create automations in HA; the REST address is derived from `JARVIS_HA_URL`/supervisor,
  using the same token). The name is always "Maga: turn off light at 23:30" («Мага: гасить свет в 23:30»), the trigger is
  `time`, and weekdays have a `weekday` condition. **"No"** is remembered forever.
- **Rollback**: deletes the automation only if the ID in our table starts with `maga_hunch_` and the name in HA starts with
  "Maga:" («Мага:» — otherwise rejected); foreign automations are not touched. Rolled-back hunches are no longer suggested.
- **Log**: "automation" and "rolled back automation" entries in the training `changelog`, visible in `what_changed`.
  `undo_change` does not roll them back (it is synchronous and only knows learned rules): rollback is done via `hunch_undo`.

## Gemini tools, console, and Telegram
`hunch_list`, `hunch_undo` (ID is optional), `hunch_answer(accept, id?)`.
Console and Telegram: `hunch list | run [N] | undo [N] | yes | no | detect | stats | on | off`.
`hunch run` = find patterns and suggest immediately without limits, `hunch run N` = create an automation for hunch N
(this is "on command", the limit of 3 does not apply). In Telegram, "yes"/"no" when a suggestion is pending go to hunches
(taking precedence over learning's "Remember?" («Запомнить?»)).

## Minor changes in common files
`brain.py` (`hunches` field), `__main__.py` (creation, `attach_ha`/`attach_lights`, shutdown, `extras`),
`telegram.py` ("yes/no" («да/нет») and `hunch`). `lights.py`, `ha.py`, `learning.py` were not changed: mocks are applied on the fly.

## No new .env variables
Enabled if HA and learning are present (i.e., voice and `GEMINI_API_KEY`). Switch: `hunch off` (pref `hunches`).

## Not verified live
- Real `POST/GET/DELETE /api/config/automation/config/<id>` via supervisor (`http://supervisor/core/api`): whether the path
  is allowed in the add-on and token permissions are sufficient. If HA returns anything other than 200, the suggestion remains
  pending and the user receives an error.
- Whether real `state_changed` events from WLED lamps and manual switches carry the necessary `context`, and whether
  the "own actions" filter doesn't let too much through (e.g., morning light from `morning.py` goes through `ha.request` and should be filtered out).
- Live Gemini reaction to the suggestion prompt and the `hunch_answer` call.
- PC work and alarm events in production data.

## Remaining / ideas
- PC/alarm/music/goodnight events are only accumulated: there are no automations for them in HA yet (later we could suggest things
  like "prepare the light before your evening sleep").
- We do not suggest automations with brightness/color: switching on without parameters.
- If a device has multiple speakers, "don't listen" («не слушай») is checked across all of them.
