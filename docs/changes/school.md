# Сессия school: школа (WebUntis, Teams/Outlook) и «помоги запомнить»

Ветка `claude/school-618b54`. Не выкладывалось и не прошивалось.

## Что выяснено про WebUntis (проверено вживую чтением под логином ученика, 2026-10-20)
* JSON-RPC `getHomeworks` и `getExams` — «Method not found»: закрыты (в JSON-RPC их не было).
* Рабочий путь: тот же вход + `GET /WebUntis/api/token/new` + Bearer, затем REST
  `/WebUntis/api/homeworks/lessons` (домашка, срок, completed, предмет по lessonId) и `/WebUntis/api/exams`
  (тип, предмет, дата, время). Живой ответ: 19 контрольных на ближайшие 200 дней, домашка за 120 дней
  (4 штуки, часть выполнена), отмены/замены/заметки через `getTimetable` (code, substText, lstext).
* Данные только читаются, пароль в логи и вывод не попадает.

## Что сделано
* `server/jarvis/school.py`: `School` (REST + getTimetable), функции Gemini `school_homework`, `school_exams`,
  `school_changes`; вечерняя подсказка через `Voice.announce` (раз в день, 19:30, вс–чт, только если есть что
  сказать, не ночью/не в «не слушай»).
* `server/jarvis/school_inbox.py`: копилка уведомлений Teams/Outlook в `brain.db` (`school_notifs`), функции
  `school_missed`, `school_tasks`, `school_task_done` (пометка «просмотрено», задания по словам
  assigned/Aufgabe/due/fällig…). Хук в мосте: `Bridge.on_notif` (2 строки).
* `server/jarvis/memorize.py`: `memorize_start/next/result`, таблицы `memo_sets`, `memo_chunks`
  (ящики Лейтнера 1/3/7/14/30 дней, ошибка — снова через 10 минут), `de_words` не тронута.
* `__main__.py`: создание и регистрация расширений (`brain.extensions`, `hub.extras`), остановка School.
* `docs/school-setup.md`: инструкция для пользователя про Microsoft Graph (путь А) и пересылку на Gmail (путь Б).
* Тесты: `server/tests/test_school.py` (13), весь набор: 441 прошло, 2 пропущено.

## Новое в `.env` (всё необязательно)
`JARVIS_SCHOOL_REMIND=on|off` (по умолчанию on), `JARVIS_SCHOOL_REMIND_AT=19:30`.
Нужны уже существующие `UNTIS_SERVER/SCHOOL/USER/PASSWORD`.

## Консоль
`school status|hw|exams|changes|missed|tasks|remind|clear`, `memo list|reset`.

## Проверить вживую
* Вечернюю фразу (`school remind` в консоли) и что она не лезет ночью/в «не слушай».
* Что уведомления Teams/Outlook реально доходят до `school_notifs` (пакеты `com.microsoft.teams`,
  `com.microsoft.office.outlook`; Teams мог бы слать другой пакет, тогда добавить в `PACKAGES`).
* Заучивание голосом: как Gemini ведёт `memorize_next/result` (подсказка Live не менялась, правила
  в описаниях функций и в `note` результата).
* Фильтр «замен»: показываются только code cancelled/irregular или substText; обычные заметки `lstext` («2/2») скрыты.

## Не сделано / вопросы
* Путь А (Graph) и Б (пересылка) только описаны: нужно согласие школы, ключей у меня нет.
* `docs/market-research.md` в этой ветке отсутствует, ориентировался на задание.
* Вечерний список «невыполненное из Teams» входит в вечернюю подсказку (число заданий) и в `school_tasks`.
