# Direct Google: Calendar, Mail, Tasks

The server accesses Google directly, without Home Assistant: `server/jarvis/google.py`. You need your own OAuth client (free, one-time) and a single browser login.

What Maga can do after connecting: «что у меня сегодня/завтра в календаре» ("what's on my calendar today/tomorrow"), «запиши встречу…» ("schedule a meeting..."), «что нового в почте» ("what's new in my email"), «добавь задачу…» ("add a task..."), «отметь задачу выполненной» ("mark the task as completed"). Email is read-only. Notes go to Google Tasks: Keep does not have an official API for personal accounts.

## Setup (One-time, ~10 Minutes)

1. Open <https://console.cloud.google.com>, at the top click «Выбор проекта» ("Select a project") → «Новый проект» ("New Project"), name it `Jarvis`.
2. «API и сервисы» ("APIs & Services") → «Библиотека» ("Library"): find and enable **Google Calendar API**, **Gmail API**, **Google Tasks API**.
3. «Google Auth Platform» (or «Экран согласия OAuth» ("OAuth consent screen")):
   - user type **«Внешний»** ("External"), name `Jarvis`, your contact email;
   - in «Аудитория» (Audience), click **«Опубликовать приложение»** ("Publish app") (In production). Otherwise, the refresh token expires after 7 days. The «приложение не проверено» ("app not verified") warning upon login is normal: «Дополнительно» ("Advanced") → «Перейти на Jarvis» ("Go to Jarvis").
4. «Клиенты» (Credentials) → «Создать клиент» ("Create credentials") → type **«Приложение для ПК» (Desktop app)**. Copy the client ID and secret into `server/.env`:

   ```
   GOOGLE_CLIENT_ID=...apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=...
   ```
5. Run the login process (a browser will open; log into your account and allow access):

   ```powershell
   cd C:\dev\jarvis\server
   .venv\Scripts\python tools\google_auth.py
   ```

   The token will be written to `server/.env` under the `GOOGLE_REFRESH_TOKEN` line; it is not printed to console output.
6. Deploy the add-on (`python tools\deploy_addon.py`, then `python tools\ha_admin.py update`): keys are pushed to it automatically.

Revoke access: <https://myaccount.google.com/permissions>. If the token stops working, Maga will say «Google не дал доступ» ("Google denied access"): simply repeat step 5.
