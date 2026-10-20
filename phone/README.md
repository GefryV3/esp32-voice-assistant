# «Колонка» ("Speaker") App (Android)

Bridge between the phone and the speaker: notifications, calls, player, alarm clock, «где мой телефон» ("where's my phone"),
headphone battery level, «открыть на компьютере» ("open on computer"), and speaker settings (volume, brightness, night, personality). Kotlin +
Jetpack Compose, styled with Material You / Material 3 Expressive (Android 16–17): colors from
wallpaper, settings groups styled like the system «Настройки» ("Settings"). Rules — `.claude/skills/material-you`.
`material3` is pinned to 1.5.0-alpha16: in stable 1.4.0 Expressive APIs are internal, while newer
builds require Android SDK 37 and AGP 9. Protocol — [docs/protocol.md](../docs/protocol.md), section
«Телефон и компьютер» ("Phone and computer").

## Build

Tools are located in `C:\dev\tools` (JDK 17, Gradle 8.11.1, Android SDK 35) and are not
installed globally into the system. The server address and token are passed to the app from `server/.env`.

```powershell
$env:JAVA_HOME = 'C:\dev\tools\jdk17'
$env:ANDROID_HOME = 'C:\dev\tools\android-sdk'
C:\dev\tools\gradle-8.11.1\bin\gradle.bat -p C:\dev\jarvis\phone assembleDebug
```

Output file: `app\build\outputs\apk\debug\app-debug.apk`. Server address is `JARVIS_SERVER`
from `server/.env` (HA addon, 192.168.178.86); a different one at build time: `-PserverHost=…`.
It can also be changed in the app itself: «Сервер» ("Server") in settings.

## Installation on the phone

1. Copy the APK to the phone (via cable or Telegram «Избранное» ("Saved Messages")) and open it.
   Android will prompt to allow installation from this source — allow it.
2. Open «Колонка» and go through the «Доступы» ("Permissions") section.
3. Android 13+ hides notification access for non-store apps:
   if it says «Ограниченная настройка» ("Restricted setting") — Settings → Apps → «Колонка» → ⋮ (top
   right) → «Разрешить ограниченные настройки» ("Allow restricted settings"), then go back to «Доступы → Уведомления» ("Permissions → Notifications").
4. OnePlus aggressively kills background apps: in addition to «Работа в фоне» ("Background work") in the app
   itself, go to Settings → Battery → «Колонка» and select «Разрешить фоновую активность» ("Allow background activity").
