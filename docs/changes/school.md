=== File: school.md ===
# Session school: school (WebUntis, Teams/Outlook) and "help me remember"

Branch `claude/school-618b54`. Neither deployed nor flashed.

## What has been figured out about WebUntis (verified live by reading under student login, 2026-10-20)
* JSON-RPC `getHomeworks` and `getExams` — "Method not found": closed (they were not present in JSON-RPC).
* Working path: same login + `GET /WebUntis/api/token/new` + Bearer, then REST
  `/WebUntis/api/homeworks/lessons` (homework, deadline, completed, subject by lessonId) and `/WebUntis/api/exams`
  (type, subject, date, time). Live response: 19 exams for the next 200 days, homework for 120 days
  (4 items, some completed), cancellations/substitutions/notes via `getTimetable` (code, substText, lstext).
* Data is read-only; the password never ends up in logs or output.

## What has been done
* `server/jarvis/school.py`: `School` (REST + getTimetable), Gemini functions `school_homework`, `school_exams`,
  `school_changes`; evening reminder via `Voice.announce` (once a day, 19:30, Sun–Thu, only if there is something
  to say, not at night/not in "do not listen" mode).
* `server/jarvis/school_inbox.py`: inbox for Teams/Outlook notifications in `brain.db` (`school_notifs`), functions
  `school_missed`, `school_tasks`, `school_task_done` (marked as "viewed", tasks filtered by keywords
  assigned/Aufgabe/due/fällig…). Hook in the bridge: `Bridge.on_notif` (2 lines).
* `server/jarvis/memorize.py`: `memorize_start/next/result`, tables `memo_sets`, `memo_chunks`
  (Leitner boxes 1/3/7/14/30 days, mistake — repeat in 10 minutes), `de_words` untouched.
* `__main__.py`: creation and registration of extensions (`brain.extensions`, `hub.extras`), stopping School.
* `docs/school-setup.md`: user instructions for Microsoft Graph (path A) and forwarding to Gmail (path B).
* Tests: `server/tests/test_school.py` (13), entire suite: 441 passed, 2 skipped.

## New in `.env` (all optional)
`JARVIS_SCHOOL_REMIND=on|off` (default on), `JARVIS_SCHOOL_REMIND_AT=19:30`.
Requires already existing `UNTIS_SERVER/SCHOOL/USER/PASSWORD`.

## Console
`school status|hw|exams|changes|missed|tasks|remind|clear`, `memo list|reset`.

## To check live
* Evening phrase (`school remind` in console) and that it doesn't trigger at night/in "do not listen" mode.
* That Teams/Outlook notifications actually reach `school_notifs` (packages `com.microsoft.teams`,
  `com.microsoft.office.outlook`; Teams might send a different package, in which case add it to `PACKAGES`).
* Voice memorization: how Gemini handles `memorize_next/result` (Live prompt unchanged, rules
  in function descriptions and in `note` of the result).
* "Substitutions" filter: only `code cancelled/irregular` or `substText` are shown; ordinary `lstext` notes ("2/2") are hidden.

## Not done / questions
* Path A (Graph) and B (forwarding) are only described: school consent is required, I don't have the keys.
* `docs/market-research.md` is absent in this branch; I relied on the assignment.
* The evening list of "uncompleted items from Teams" is included in the evening reminder (number of tasks) and in `school_tasks`.
