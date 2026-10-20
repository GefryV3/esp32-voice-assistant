# phone-haptics: haptic feedback and app build 0.4.0

## Done
- `phone/.../ui/Haptics.kt`: a single `rememberHaptics()` helper (tick, step, confirm, click, toggle, reject).
  Disabled via `Prefs.haptics` (enabled by default). The system setting «Отклик при касании» ("Touch feedback")
  is handled by Android itself (View's `performHapticFeedback`).
- Sliders (`SliderRow`): `SegmentFrequentTick` on every 1% step, `Confirm` on release.
  Toggles: `ToggleOn/ToggleOff`. `SettingRow` tiles with onClick and `HapticTonalButton`: `ContextClick`.
  «−»/«+» and segments: `SegmentTick`. `Reject`: on invalid server address (not `ws…`) in the dialog.
- Settings → «Связь» ("Connectivity") → «Виброотклик» ("Haptic feedback") toggle.
- versionName 0.4.0 (versionCode 4).

## Build
- Built without code changes: unverified code (cover art palette, SET_ALARM, set_alarm) compiles.
- Gradle crashed due to out of memory (flashing and pytest from other sessions were running on the machine): solved by
  `--no-daemon --max-workers=1 -Dorg.gradle.jvmargs="-Xmx1g -XX:MaxMetaspaceSize=384m"
  -Pkotlin.compiler.execution.strategy=in-process`; didn't change gradle.properties.
- For the token in BuildConfig, `server/.env` was copied into the worktree (not tracked in git).
- APK: `C:\dev\jarvis\phone\Kolonka-0.4.0.apk`, 9.39 MB (not committed to git, *.apk in .gitignore).
- R8: the rule `-keep class dev.jarvis.** { *; }` keeps our code; `mapping.txt` contains Bridge, Media,
  NotifListener, Haptics, androidx.palette.graphics.Palette (+Swatch/Builder), SET_ALARM in manifest.

## Not verified on phone
- How the vibrations feel (especially SegmentFrequentTick on the slider, whether it feels too frequent).
- That the cover art palette actually tints the UI, that the alarm gets set via `set_alarm`/`alarm_set`.
- Running the APK on OnePlus 13s after R8.

## 0.4.1: sliders redesigned (based on feedback: felt weak/unpleasant)
What was wrong: a `SegmentFrequentTick` tick on every 1% (up to 100 vibrations per drag, rate-unlimited).
`SEGMENT_FREQUENT_TICK` is designed for very frequent events and is intentionally barely perceptible, and a stream of 100 of them produces a "buzzing" sensation.
What was done (`SliderHaptics` class in `ui/Haptics.kt`), based on documentation pages:
- [Haptic design principles](https://developer.android.com/develop/ui/views/haptics/haptics-principles): frequent events should be very soft,
  discrete short ticks, stronger when "snapping" to a target; no vibration is better than a "buzz".
- [Haptic feedback for common use cases](https://developer.android.com/develop/ui/views/haptics/haptic-feedback): `View.performHapticFeedback`
  with `HapticFeedbackConstants`, respects system «Вибрацию при касании» ("Touch vibration"); did not set `FLAG_IGNORE_GLOBAL_SETTING`.
- [Custom haptic effects](https://developer.android.com/develop/ui/views/haptics/custom-haptic-effects): interval between events ≥ ~50 ms so they remain distinct.
Summary:
- slider step = 5% (20 steps), tick (`SEGMENT_TICK`, `CLOCK_TICK` on API < 34) only on step change and no more than once every 40 ms;
- at edges (0% and 100%) and at default values (volume 50, snark 60), more pronounced: `GESTURE_THRESHOLD_ACTIVATE` (API 34+, otherwise `CONFIRM`); this event is not throttled;
- on release: `CONFIRM`;
- buttons, toggles, «−/+» unchanged. Did not use primitive compositions (`VibrationEffect.Composition`): they require `VIBRATE` and an `areAllPrimitivesSupported` check, whereas View haptics covers the task and respects the system setting automatically.
Not verified on phone: feel; if the tick is too weak — adjust `minGapMs`/`steps` in `SliderHaptics`, if it is still too harsh — switch back to `SEGMENT_FREQUENT_TICK`.
APK: `C:\dev\jarvis\phone\Kolonka-0.4.1.apk` (9.39 MB, not in git).
