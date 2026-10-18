package dev.jarvis.bridge.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.ExperimentalTextApi
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontVariation
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp
import dev.jarvis.bridge.R

// Оформление по Apple HIG (iOS): сгруппированные карточки, системные цвета для светлой и
// тёмной темы, шкала текста Large Title / Body / Footnote. Размеры в sp — следуют
// системному масштабу шрифта, как Dynamic Type.

data class Palette(
    val bg: Color,         // systemGroupedBackground
    val card: Color,       // secondarySystemGroupedBackground
    val label: Color,
    val secondary: Color,  // secondaryLabel
    val tertiary: Color,   // tertiaryLabel
    val separator: Color,
    val fill: Color,       // tertiarySystemFill — дорожки, сегменты
    val switchOff: Color,
    val segment: Color,    // выбранный сегмент
    val blue: Color,
    val green: Color,
    val red: Color,
    val orange: Color,
    val dark: Boolean,
)

val Light = Palette(
    bg = Color(0xFFF2F2F7), card = Color.White, label = Color.Black,
    secondary = Color(0x993C3C43), tertiary = Color(0x4D3C3C43), separator = Color(0x4A3C3C43),
    fill = Color(0x1F767680), switchOff = Color(0xFFE9E9EA), segment = Color.White,
    blue = Color(0xFF007AFF), green = Color(0xFF34C759), red = Color(0xFFFF3B30), orange = Color(0xFFFF9500),
    dark = false,
)

val Dark = Palette(
    bg = Color.Black, card = Color(0xFF1C1C1E), label = Color.White,
    secondary = Color(0x99EBEBF5), tertiary = Color(0x4DEBEBF5), separator = Color(0x99545458),
    fill = Color(0x3D767680), switchOff = Color(0xFF39393D), segment = Color(0xFF636366),
    blue = Color(0xFF0A84FF), green = Color(0xFF30D158), red = Color(0xFFFF453A), orange = Color(0xFFFF9F0A),
    dark = true,
)

val LocalPalette = staticCompositionLocalOf { Light }

@OptIn(ExperimentalTextApi::class)
private fun inter(w: Int) = Font(
    R.font.inter,
    weight = FontWeight(w),
    variationSettings = FontVariation.Settings(FontVariation.weight(w)),
)

/** Inter — ближайший открытый родственник SF Pro, с кириллицей. */
val Inter = FontFamily(inter(400), inter(500), inter(600), inter(700))

object Type {
    val largeTitle = TextStyle(fontFamily = Inter, fontWeight = FontWeight.Bold, fontSize = 34.sp, lineHeight = 41.sp, letterSpacing = 0.01.em)
    val title3 = TextStyle(fontFamily = Inter, fontWeight = FontWeight.SemiBold, fontSize = 20.sp, lineHeight = 25.sp)
    val body = TextStyle(fontFamily = Inter, fontWeight = FontWeight.Normal, fontSize = 17.sp, lineHeight = 22.sp, letterSpacing = (-0.02).em)
    val bodyStrong = body.copy(fontWeight = FontWeight.SemiBold)
    val subhead = TextStyle(fontFamily = Inter, fontWeight = FontWeight.Normal, fontSize = 15.sp, lineHeight = 20.sp, letterSpacing = (-0.01).em)
    val footnote = TextStyle(fontFamily = Inter, fontWeight = FontWeight.Normal, fontSize = 13.sp, lineHeight = 18.sp)
    val caption = TextStyle(fontFamily = Inter, fontWeight = FontWeight.Medium, fontSize = 12.sp, lineHeight = 16.sp)
}

@Composable
fun AppTheme(content: @Composable () -> Unit) {
    CompositionLocalProvider(LocalPalette provides if (isSystemInDarkTheme()) Dark else Light) {
        content()
    }
}
