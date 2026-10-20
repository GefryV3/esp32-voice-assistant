# School: What is Connected and How to Connect the Rest

Maga knows about the school from three sources. Everything is read-only, without bypassing school restrictions.

## 1. WebUntis (ready, keys already in `server/.env`)

You need `UNTIS_SERVER`, `UNTIS_SCHOOL`, `UNTIS_USER`, `UNTIS_PASSWORD` (student login).
The "iCal only" mode (`UNTIS_ICAL_URL`) provides the schedule, but not homework and exams.

* "what is assigned" ("что задано") → `school_homework`, "when is the exam" ("когда контрольная") → `school_exams`, "what was cancelled tomorrow" ("что завтра отменили") → `school_changes`.
* In the evening at 19:30 (Sun–Thu, on the eve of a school day), Maga will remind you himself, but only if there is something to say:
  tomorrow's/the day after tomorrow's exam, uncompleted homework, cancellations and substitutions, Teams assignments without a mark.
  Not at night and not in the "do not listen" ("не слушай") mode. Once a day.
* `.env`: `JARVIS_SCHOOL_REMIND=on|off`, `JARVIS_SCHOOL_REMIND_AT=19:30`.
* Console: `school status|hw|exams|changes|missed|tasks|remind|clear`.

## 2. Teams and Outlook notifications from the phone (ready, nothing to set up at school)

Maga's app on the phone already forwards notifications to the server (notification access is enabled). The server
saves those that came from `com.microsoft.teams` and `com.microsoft.office.outlook` (the `school_notifs` table).

* "what did I miss in Teams and mail" ("что я пропустил в Teams и почте") → `school_missed` (after showing the record, they are marked as "viewed" ("просмотрено")).
* "what is not submitted" ("что не сдано") → `school_tasks`: notifications with the words assigned / Aufgabe / due / fällig / zugewiesen / Abgabe;
  they hang around until you say "done" ("сделал") (`school_task_done`).
* Conditions: in Maga's app on the phone, Teams and Outlook are **not muted** (the "Apps" ("Приложения") screen), notifications are enabled in Teams/Outlook themselves, the phone is connected to the server. What came while the phone was turned off or without a network will not arrive: only what the phone showed while the bridge was running is accumulated.
* Limitation: the notification text is short (often just the title and the first line), so this is "what came",
  not a full list of assignments. The full list below (path A) is only possible with the school's consent.

## 3. Path A: Microsoft Graph (mail and Teams assignments directly) — only with the school's consent

What will be needed according to the Microsoft Graph documentation (delegated permissions, student login):
* mail: `Mail.Read`;
* Teams assignments (Education API): `EduAssignments.ReadBasic` (for the student: their assignments, deadlines, submission status);
* `offline_access` for a long-term login.

For the application to be able to log into the school account, application registration in Microsoft Entra ID is required
(the entra.microsoft.com portal). **As a rule, a student of a school tenant has application registration and consent to permissions disabled**; permissions like `EduAssignments.*` require administrator consent in any case.
Therefore, realistically it is as follows:
1. Ask the school IT administrator (at HTL this is usually the IT admin/Systembetreuer) whether access
   of third-party applications to Teams assignments and student mail is allowed and whether administrator consent can be issued.
2. If yes: the administrator registers the application (type "public client", login via device code),
   issues `Mail.Read` and `EduAssignments.ReadBasic`, sends you the Client ID and Tenant ID (these are not passwords).
3. Then you enter the keys (`MS_CLIENT_ID`, `MS_TENANT_ID`) into `server/.env` yourself, and I will write the module.
If the school refused, we do not bypass anything: path B and notifications remain (section 2).

## 4. Path B: Forwarding school mail to Gmail

Maga already has Gmail connected (`server/jarvis/google.py`), so emails forwarded to it will be read by him,
and school emails can be distinguished by the sender (school address).
1. Open school mail in a browser: outlook.office.com (login with school account).
2. Gear "Settings" ("Параметры") → "Mail" ("Почта") → "Forwarding" ("Пересылка"). Enable forwarding, specify your Gmail,
   optionally "Keep a copy" ("Сохранять копию").
   Or "Mail" → "Rules" ("Правила") → "Add new rule" ("Добавить правило") → action "Forward to..." ("Переслать на…").
3. If forwarding is grayed out or says it is prohibited by the administrator: the school has disabled it (this often happens).
   Do not bypass it: either ask the IT administrator or use notifications (section 2).
4. Gmail may first ask you to confirm forwarding: a confirmation email arrives on Gmail.
5. Check: ask a school friend or yourself to send an email and ask Maga "what came to the mail" ("что пришло на почту").
Disadvantage: only emails are forwarded, and Teams assignments (this is not mail) are not: for them, use section 2 or path A.
