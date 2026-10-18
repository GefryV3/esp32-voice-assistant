---
name: material-you
description: Дизайн Android-приложения в стиле Material You / Material 3 Expressive (Android 16–17) на Jetpack Compose. Использовать при любой правке интерфейса phone/ — экраны, компоненты, цвета, шрифты, анимации — и когда просят «как в Android», «Material», «Material You», «M3».
---

# Material You / Material 3 Expressive для приложения «Колонка»

Цель — чтобы приложение выглядело как системные приложения Pixel на Android 16–17: цвета из
обоев, крупные скругления, пружинная анимация, стандартные компоненты Material 3.

## Библиотеки

- Compose BOM `2025.10.00` → `material3` 1.4.0 (в ней Expressive API, они помечены
  `@ExperimentalMaterial3ExpressiveApi` — включать `@OptIn` на уровне файла).
- Хватает compileSdk 35 и AGP 8.6+ (у нас 8.7.3, переносной набор в `C:\dev\tools`).
- Иконки — `androidx.compose.material:material-icons-core` (лёгкий набор). `extended` не
  брать: без R8 он раздувает APK на десятки мегабайт.

## Тема

- `MaterialExpressiveTheme(colorScheme, motionScheme = MotionScheme.expressive())`.
- Цвета: `dynamicLightColorScheme(ctx)` / `dynamicDarkColorScheme(ctx)` (minSdk 31 — всегда
  доступны). Свои цвета — только для смысла, которого нет в схеме (например «на связи» —
  берём `tertiary`, «нет связи» — `error`). Никаких захардкоженных hex в экранах.
- Шрифт — системный (типографика `MaterialTheme.typography` по умолчанию). Заголовок экрана
  — `displaySmall`/`headlineLarge`, строки — `bodyLarge`, подписи — `bodyMedium`/`labelLarge`.
- Фон экрана — `surfaceContainer` (в тёмной теме почти чёрный с оттенком обоев), карточки —
  `surfaceBright`/`surfaceContainerHighest`.

## Раскладка экрана настроек (как «Настройки» Android 16)

- `Scaffold` + `LargeFlexibleTopAppBar` (крупный заголовок, при прокрутке сжимается):
  `TopAppBarDefaults.exitUntilCollapsedScrollBehavior()` и `nestedScroll`.
- Edge-to-edge: `enableEdgeToEdge()`, отступы из `innerPadding` Scaffold.
- Настройки — **группы**: заголовок группы `labelLarge` цветом `primary` с отступом 16 dp,
  под ним строки-карточки с зазором 2 dp. Скругление: у группы снаружи 24 dp, между
  строками 4 dp (первая строка — большие верхние углы, последняя — нижние, одна — все).
- Строка: `ListItem` на прозрачном фоне внутри `Surface` строки; высота от 56 dp
  (с подписью — 72), отступы 16 dp; ведущая иконка в тональном кружке 40 dp
  (`secondaryContainer`/`onSecondaryContainer`).
- Пояснение под группой — `bodySmall`, `onSurfaceVariant`.

## Компоненты

- Переключатель — `Switch` с иконкой галочки в бегунке (`thumbContent` при checked).
- Выбор из 2–3 — `SingleChoiceSegmentedButtonRow` + `SegmentedButton`.
- Число/время «±» — пара `FilledTonalIconButton` (или `OutlinedIconButton`), значение
  крупно рядом (`titleMedium`).
- Ползунок — `Slider` (в 1.4 толстая дорожка и вертикальный бегунок — стиль Expressive);
  значение меняется на экране сразу, на сервер — в `onValueChangeFinished`.
- Действия — `FilledTonalButton` / `Button`, не «синий текст» из iOS.
- Диалоги — `AlertDialog` с `TextButton`.
- Статус — `AssistChip`/маленький `Badge`-кружок цветом `tertiary`/`error`.

## Движение и отклик

- Анимации — пружины из `MaterialTheme.motionScheme` (`defaultSpatialSpec()`,
  `fastEffectsSpec()`), не `tween`.
- Нажатия — системная волна (ripple) и `HapticFeedbackType.ToggleOn/ToggleOff`/
  `SegmentTick` на переключателях и шагах.
- Формы для «героя» и иллюстраций — `MaterialShapes` (например `Cookie9Sided`,
  `Sunny`) через `.toShape()`.

## Доступность

- Размеры в sp/dp, тексты переносятся (без жёсткой высоты строки).
- У переключателей и ползунков — `contentDescription`/`stateDescription` по-русски.
- Контраст обеспечивает динамическая схема — не перекрашивать текст вручную.

## Сборка и проверка

```powershell
$env:JAVA_HOME = 'C:\dev\tools\jdk17'; $env:ANDROID_HOME = 'C:\dev\tools\android-sdk'
C:\dev\tools\gradle-8.11.1\bin\gradle.bat -p C:\dev\jarvis\phone assembleDebug
```

APK — `phone/app/build/outputs/apk/debug/app-debug.apk`; переименовать в
`phone/Kolonka-<версия>.apk` и отправить пользователю через SendUserFile.
