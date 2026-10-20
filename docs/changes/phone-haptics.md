# phone-haptics: виброотклик и сборка приложения 0.4.0

## Сделано
- `phone/.../ui/Haptics.kt`: один помощник `rememberHaptics()` (tick, step, confirm, click, toggle, reject).
  Выключается `Prefs.haptics` (по умолчанию включено). Системная настройка «Отклик при касании»
  учитывается самим Android (performHapticFeedback у View).
- Слайдеры (`SliderRow`): `SegmentFrequentTick` на каждом шаге в 1 %, `Confirm` при отпускании.
  Переключатели: `ToggleOn/ToggleOff`. Плитки `SettingRow` с onClick и `HapticTonalButton`: `ContextClick`.
  «−»/«+» и сегменты: `SegmentTick`. `Reject`: при неверном адресе сервера (не `ws…`) в диалоге.
- Настройки → «Связь» → переключатель «Виброотклик».
- versionName 0.4.0 (versionCode 4).

## Сборка
- Собралось без правок кода: непроверенный код (палитра обложки, SET_ALARM, set_alarm) компилируется.
- Gradle падал по нехватке памяти (на машине шли прошивка и pytest других сессий): помогло
  `--no-daemon --max-workers=1 -Dorg.gradle.jvmargs="-Xmx1g -XX:MaxMetaspaceSize=384m"
  -Pkotlin.compiler.execution.strategy=in-process`, gradle.properties не менял.
- Для токена в BuildConfig в worktree копировался `server/.env` (в git не попадает).
- APK: `C:\dev\jarvis\phone\Kolonka-0.4.0.apk`, 9,39 МБ (в git не коммитится, *.apk в .gitignore).
- R8: правило `-keep class dev.jarvis.** { *; }` держит наш код; в mapping.txt есть Bridge, Media,
  NotifListener, Haptics, androidx.palette.graphics.Palette (+Swatch/Builder), SET_ALARM в манифесте.

## Не проверено на телефоне
- Как ощущаются вибрации (особенно SegmentFrequentTick на ползунке, не слишком ли часто).
- Что палитра обложки реально красит интерфейс, что будильник ставится по `set_alarm`/`alarm_set`.
- Запуск APK на OnePlus 13s после R8.
