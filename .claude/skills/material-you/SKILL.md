---
name: material-you
description: Android app design in Material You / Material 3 Expressive (Android 16–17) style using Jetpack Compose. Use for any UI modifications in phone/ — screens, components, colors, fonts, animations — and whenever asked for «как в Android» ("like in Android"), «Material», «Material You», or «M3».
---

# Material You / Material 3 Expressive for the «Колонка» ("Smart Speaker") App

The goal is for the app to look like Pixel system apps on Android 16–17: wallpaper-based colors, large corner radiuses, spring animations, standard Material 3 components.

## Libraries

- Compose BOM `2025.10.00` → `material3` 1.4.0 (contains Expressive APIs, marked with
  `@ExperimentalMaterial3ExpressiveApi` — enable `@OptIn` at the file level).
- compileSdk 35 and AGP 8.6+ are sufficient (we use 8.7.3, portable toolchain in `C:\dev\tools`).
- Icons — `androidx.compose.material:material-icons-core` (lightweight set). Do not use
  `extended`: without R8, it bloats the APK by dozens of megabytes.

## Theme

- `MaterialExpressiveTheme(colorScheme, motionScheme = MotionScheme.expressive())`.
- Colors: `dynamicLightColorScheme(ctx)` / `dynamicDarkColorScheme(ctx)` (minSdk 31 — always
  available). Custom colors — only for semantics not present in the scheme (for example,
  «на связи» ("connected") — use `tertiary`, «нет связи» ("no connection") — `error`).
  No hardcoded hex values in screens.
- Font — system default (typography `MaterialTheme.typography` by default). Screen title
  — `displaySmall`/`headlineLarge`, rows — `bodyLarge`, captions — `bodyMedium`/`labelLarge`.
- Screen background — `surfaceContainer` (almost black with a wallpaper tint in dark theme),
  cards — `surfaceBright`/`surfaceContainerHighest`.

## Settings Screen Layout (Like Android 16 «Настройки» ("Settings"))

- `Scaffold` + `LargeFlexibleTopAppBar` (large title, collapses on scroll):
  `TopAppBarDefaults.exitUntilCollapsedScrollBehavior()` and `nestedScroll`.
- Edge-to-edge: `enableEdgeToEdge()`, insets from `Scaffold`'s `innerPadding`.
- Settings — **groups**: group header `labelLarge` in `primary` color with a 16 dp indent,
  followed by card rows with a 2 dp gap. Corner radius: 24 dp on the outside of the group,
  4 dp between rows (first row — large top corners, last row — bottom corners, single row — all corners).
- Row: `ListItem` on a transparent background inside the row's `Surface`; height from 56 dp
  (with caption — 72), 16 dp padding; leading icon inside a 40 dp tonal circle
  (`secondaryContainer`/`onSecondaryContainer`).
- Helper text under the group — `bodySmall`, `onSurfaceVariant`.

## Components

- Switch — `Switch` with a checkmark icon in the thumb (`thumbContent` when checked).
- Choice of 2–3 — `SingleChoiceSegmentedButtonRow` + `SegmentedButton`.
- Number/time «±» ("+/-") — a pair of `FilledTonalIconButton` (or `OutlinedIconButton`), value
  displayed prominently nearby (`titleMedium`).
- Slider — `Slider` (in 1.4, it has a thick track and vertical thumb — Expressive style);
  the value updates on screen immediately, sent to the server in `onValueChangeFinished`.
- Actions — `FilledTonalButton` / `Button`, not iOS-style "blue text".
- Dialogs — `AlertDialog` with `TextButton`.
- Status — `AssistChip`/small `Badge` circle colored with `tertiary`/`error`.

## Motion and Feedback

- Animations — springs from `MaterialTheme.motionScheme` (`defaultSpatialSpec()`,
  `fastEffectsSpec()`), not `tween`.
- Taps — system ripple and `HapticFeedbackType.ToggleOn/ToggleOff`/
  `SegmentTick` on switches and steps.
- Shapes for "hero" elements and illustrations — `MaterialShapes` (for example,
  `Cookie9Sided`, `Sunny`) via `.toShape()`.

## Accessibility

- Dimensions in sp/dp, text wraps (no hardcoded line height).
- Switches and sliders must have `contentDescription`/`stateDescription` in Russian.
- Contrast is ensured by the dynamic color scheme — do not recolor text manually.

## Build and Verification

```powershell
$env:JAVA_HOME = 'C:\dev\tools\jdk17'; $env:ANDROID_HOME = 'C:\dev\tools\android-sdk'
C:\dev\tools\gradle-8.11.1\bin\gradle.bat -p C:\dev\jarvis\phone assembleDebug
```

APK — `phone/app/build/outputs/apk/debug/app-debug.apk`; rename to
`phone/Kolonka-<версия>.apk` and send to the user via SendUserFile.
