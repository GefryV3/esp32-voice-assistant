# Morning pack (#8, #9, #27, #28)

Branch `claude/morning-pack-7a6f31`. Tests: `server/tests/test_morning.py` (33 mock-based tests), full suite 228 passed, 2 skipped.

## What was done

**#9 Schedule-based alarm clock** — `server/jarvis/untis.py`, `morning.py`.
- Two ways to fetch the schedule behind a single `Untis.lessons(day)` interface: an iCal link «Календарь-Abo» ("Calendar subscription")
  (`UNTIS_ICAL_URL`, preferred: no password required) or WebUntis JSON-RPC with student credentials
  (`UNTIS_SERVER/SCHOOL/USER/PASSWORD`). Cancellations (STATUS:CANCELLED, «Entfall», `code=cancelled`) are skipped,
  substitutions are marked. Timezone: Windows lacks tzdata, so there is a fallback EU daylight saving time calculation.
- Wake-up time = start of the first held lesson − travel time − morning prep. Travel time = `WAKE_SCHOOL_START` − `WAKE_BUS_DEPART`
  (08:00 − 07:19 = 41 min). If `TRANSIT_STOP` is configured, the actual latest departure not later than the calculated
  departure is used. Rounded down to 5 minutes, no earlier than `WAKE_EARLIEST` (05:00). For an 8:00 lesson with 60 min prep time,
  it comes out to 06:15.
- In the evening (from `WAKE_ASK_AT` 21:30 to 23:00, once a day, survives restarts), Maga suggests via `announce`:
  «завтра подъём в 06:15, ставлю?» ("wake up tomorrow is at 06:15, should I set it?"). If an alarm within 5 minutes is already set on the phone, or there are
  no lessons tomorrow, it stays silent. Gemini tools: `set_wake_alarm(time)`, `wake_plan()`.
- Alarm on the phone: new server → phone message `set_alarm` (`hour`, `minute`, `label`), handler
  in `phone/.../Bridge.kt` (Android `AlarmClock.ACTION_SET_ALARM`, `SKIP_UI`), `SET_ALARM` permission in the manifest.
  If Android doesn't allow opening the clock from the background, a notification «Поставить будильник на 06:15» ("Set alarm for 06:15") appears; tapping it sets the alarm.
  The phone responds with `alarm_set` (`direct`); the server doesn't read it yet. Described in `docs/protocol.md`.
- **Sunrise for the alarm** (HA automation 1756302078808, currently 06:10 Mon-Fri) left unchanged. Proposal: create
  `input_datetime.jarvis_wake` in HA (time only), replace the automation trigger with "time = this helper minus
  20 minutes" (`time` trigger with `offset: -00:20:00`, `at: input_datetime.jarvis_wake`), and set
  `WAKE_HA_ENTITY=input_datetime.jarvis_wake` in `.env`. The server will set the time whenever an alarm is scheduled. Did not flash or deploy.

**#8 Soft wake-up** — changes in `routines.py`. After `SOFT_WAKE_DELAY_S` (30) following `alarm dismissed`, Maga
quietly says «доброе утро, время, погода, первый урок, автобус» ("good morning, time, weather, first lesson, bus"), and this also counts as the day's morning briefing
(it won't speak a second time at 08:00). Also works during "night" hours before 07:00. `SOFT_WAKE=off` restores the old behavior.
«Точно встал» ("Definitely awake") (`bridge._watch_alarm`) left untouched. Console: `routine wake`.

**#27 Bus** — `server/jarvis/transit.py`. Source: HAFAS ÖBB (`fahrplan.oebb.at/bin/mgate.exe`), covers VOR,
real-time data, and cancellations, no API key or registration required. **Tested live**: search by name finds stops, departures
include line numbers. The protocol is unofficial (public `aid` extracted from the ÖBB app), may change; the endpoint
is configurable via `TRANSIT_URL`. Public `v6.oebb.transport.rest` did not respond (TLS error), so I did not rely on it. In the morning, starting
`BUS_WATCH_MIN` (40) minutes before departure and as long as the user is awake, the trip is checked every 3 minutes; a delay of 3+ minutes
or a cancellation is announced once per change (upon cancellation, the next trip is announced). Gemini function `next_bus`
(«когда мой автобус» ("when is my bus")). Console: `morning bus`.

**#28 Umbrella** — `server/jarvis/umbrella.py`. Open-Meteo without an API key (tested live), coordinates `WEATHER_LAT/LON`
(defaults to central Vienna, custom coordinates in `.env`). When HA reports `person.*` → not_home
(`on_leave` hook in routines) and rain probability in the next 8 hours is ≥ 50% or precipitation ≥ 0.3 mm/h, Maga reminds you about an umbrella;
no more often than once every 3 hours, not at night. `UMBRELLA=off` disables it.

## New .env variables
All in `server/.env.example` («Утренний пакет» ("Morning pack") block). Main ones: `UNTIS_ICAL_URL` (or the four `UNTIS_*`),
`TRANSIT_STOP`, `TRANSIT_LINE`, `WAKE_PREP_MIN`, `WAKE_BUS_DEPART`, `WAKE_HA_ENTITY`, `SOFT_WAKE`, `BUS_WARN`, `UMBRELLA`.
Read in `__main__` directly from `.env`/environment (`config.py` left unchanged). In the HA add-on, these variables
need to be passed through just like the rest (`addon/config.yaml` left untouched).

## Questions for the user (what to provide)
1. **The «Календарь-Abo» link** from WebUntis (My profile/Stundenplan → «Kalender abonnieren» / iCal-Link) and add
   it to `.env` as `UNTIS_ICAL_URL`. The link is secret, do not post it in the chat: enter it yourself. If the school doesn't provide such a link:
   WebUntis server (`xxx.webuntis.com`), school code, and student username/password, also in `.env`.
2. **The exact stop name** (as in the ÖBB/anachb app, e.g., "Musterdorf Hauptplatz" or simply
   "Musterdorf") and the **line number** you take at 7:19 (e.g., 123) for `TRANSIT_STOP`, `TRANSIT_LINE`.
3. How many minutes for morning prep (default is 60) and whether the 7:19 bus indeed gets you there for 8:00 (`WAKE_SCHOOL_START`).
4. Whether you like the `input_datetime` idea for «Рассвет» ("Sunrise"): let me know, and I (or the coordinator) will implement it.

## Not tested live
Everything related to the phone and speaker: `set_alarm` on a real Android device (especially launching the clock from background), voice
confirmation «да, ставь» ("yes, set it"), quiet voice during soft wake-up, triggering of the departure hook (HA person delay ≈ leaving the home zone),
format of a real WebUntis iCal link (parsed according to the iCal standard, not against an actual file) and real
JSON-RPC response (based on documentation). Did not build the phone/ app (did not run Gradle).

## Remaining
- The server does not read `alarm_set` from the phone (confirmation for Maga).
- Duplicates: each `set_alarm` creates a new alarm in the Android clock; old ones are not deleted (Android does not allow this). The server
  sets it once a day, but in case of «передумал, ставь на другое» ("changed my mind, set it for another time"), there will be two.
- «Телефон ушёл» ("Phone departed") as a second departure trigger for the umbrella is not implemented, only HA person.
