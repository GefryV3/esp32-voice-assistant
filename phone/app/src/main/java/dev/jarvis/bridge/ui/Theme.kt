@file:OptIn(ExperimentalMaterial3ExpressiveApi::class)

package dev.jarvis.bridge.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.MaterialExpressiveTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MotionScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext

// Material You / Material 3 Expressive (Android 16–17): цвета из обоев телефона, системный
// шрифт, пружинная анимация. Правила — .claude/skills/material-you.

@Composable
fun AppTheme(content: @Composable () -> Unit) {
    val ctx = LocalContext.current
    val scheme = if (isSystemInDarkTheme()) dynamicDarkColorScheme(ctx) else dynamicLightColorScheme(ctx)
    MaterialExpressiveTheme(colorScheme = scheme, motionScheme = MotionScheme.expressive(), content = content)
}

/** Цвета состояния связи — из той же динамической схемы, без своих hex. */
object Status {
    val ok: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.tertiary
    val warn: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.secondary
    val bad: Color @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.error
}

val ColorScheme.screen: Color get() = surfaceContainer
val ColorScheme.row: Color get() = surfaceBright
