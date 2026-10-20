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

## 0.4.1: слайдеры переделаны (по жалобе: ощущались слабо/неприятно)
Что не так было: тик `SegmentFrequentTick` на каждый 1 % (до 100 вибраций за протяжку, без ограничения частоты).
`SEGMENT_FREQUENT_TICK` рассчитан на очень частые события и намеренно еле заметен, а поток из 100 штук даёт «жужжание».
Что сделано (класс `SliderHaptics` в `ui/Haptics.kt`), по страницам:
- [Haptic design principles](https://developer.android.com/develop/ui/views/haptics/haptics-principles): частые события — очень мягкие,
  отдельные короткие тики, сильнее при «прилипании» к цели; лучше без вибрации, чем «жужжание».
- [Haptic feedback for common use cases](https://developer.android.com/develop/ui/views/haptics/haptic-feedback): `View.performHapticFeedback`
  с `HapticFeedbackConstants`, уважает системную «Вибрацию при касании»; `FLAG_IGNORE_GLOBAL_SETTING` не ставил.
- [Custom haptic effects](https://developer.android.com/develop/ui/views/haptics/custom-haptic-effects): интервал между событиями ≥ ~50 мс, чтобы они различались.
Итог:
- шаг ползунка = 5 % (20 шагов), тик (`SEGMENT_TICK`, на API<34 `CLOCK_TICK`) только при смене шага и не чаще чем раз в 40 мс;
- на краях (0 % и 100 %) и на значении по умолчанию (громкость 50, язвительность 60) заметнее: `GESTURE_THRESHOLD_ACTIVATE` (API 34+, иначе `CONFIRM`), это событие не режется лимитом;
- при отпускании `CONFIRM`;
- кнопки, переключатели, «−/+» не менял. Композиции примитивов (`VibrationEffect.Composition`) не использовал: нужны `VIBRATE` и проверка `areAllPrimitivesSupported`, а view-хаптика покрывает задачу и сама учитывает системную настройку.
Не проверено на телефоне: ощущение; если тик слабый — `minGapMs`/`steps` в `SliderHaptics`, если всё ещё резко — взять `SEGMENT_FREQUENT_TICK` обратно.
APK: `C:\dev\jarvis\phone\Kolonka-0.4.1.apk` (9,39 МБ, в git нет).
