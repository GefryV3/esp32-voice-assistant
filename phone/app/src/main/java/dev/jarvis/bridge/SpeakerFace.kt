package dev.jarvis.bridge

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.random.Random

// Колонка в приложении — такой, какая она сейчас: те же почти чёрный экран, светящиеся глаза,
// часы и сияние по краю, что рисует прошивка (design/screen-mockup.html). Цвета здесь —
// цвета самой колонки, а не темы телефона: это её портрет.

private val Bg = Color(0xFF05070B)
private val Eye = Color(0xFFE2ECFF)
private val EyeGlow = Color(0xFF4080FF)
private val Dim = Color(0xFF7C8494)
private val NightRed = Color(0xFFB0342A)
private val Ring = listOf(Color(0xFF4F8CFF), Color(0xFFB06BFF), Color(0xFFFF6FA8), Color(0xFF6FD3FF), Color(0xFF4F8CFF))

@Composable
fun SpeakerFace(screen: ScreenState, size: Dp = 196.dp) {
    val blink = remember { Animatable(1f) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(2500L + Random.nextLong(3500))
            blink.animateTo(0.08f, tween(70))
            blink.animateTo(1f, tween(150))
        }
    }
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) {
        while (true) {
            now = System.currentTimeMillis()
            delay(1000)
        }
    }
    val spin = rememberInfiniteTransition(label = "edge")
    val angle by spin.animateFloat(0f, 360f, infiniteRepeatable(tween(3200, easing = LinearEasing)), label = "angle")
    val pulse by spin.animateFloat(0.7f, 1f, infiniteRepeatable(tween(800), RepeatMode.Reverse), label = "pulse")

    val mode = screen.mode
    val talking = mode == "listening" || mode == "thinking" || mode == "speaking"
    val clock = SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(now))

    Box(
        Modifier.size(size).semantics { contentDescription = "Колонка: ${screen.title}" },
        contentAlignment = Alignment.Center,
    ) {
        Canvas(Modifier.fillMaxSize()) {
            val r = this.size.minDimension / 2
            drawCircle(Bg, r)
            drawCircle(Color(0xFF1A1E26), r - 1.dp.toPx(), style = Stroke(2.dp.toPx()))
            if (talking) {   // сияние по краю, как в прошивке: бежит по кругу и дышит
                rotate(angle) {
                    drawCircle(Brush.sweepGradient(Ring), r - 4.dp.toPx(), alpha = 0.35f * pulse, style = Stroke(10.dp.toPx()))
                    drawCircle(Brush.sweepGradient(Ring), r - 3.dp.toPx(), alpha = pulse, style = Stroke(3.dp.toPx()))
                }
            }
            if (mode == "timer" && screen.until > now) {   // таймер — дуга по краю
                drawArc(EyeGlow.copy(alpha = 0.9f), -90f, 360f * (((screen.until - now) / 1000f) % 3600f) / 3600f, false,
                        topLeft = Offset(6.dp.toPx(), 6.dp.toPx()), size = Size(2 * r - 12.dp.toPx(), 2 * r - 12.dp.toPx()),
                        style = Stroke(5.dp.toPx(), cap = androidx.compose.ui.graphics.StrokeCap.Round))
            }
            // глаза
            val ew = this.size.width * 0.13f
            val asleep = mode == "sleep" || mode == "night"
            val eh = when {
                asleep -> this.size.height * 0.022f
                mode == "speaking" -> this.size.height * 0.17f
                else -> this.size.height * 0.2f * blink.value
            }
            val eyeColor = when (mode) {
                "night" -> NightRed
                "offline", "sleep" -> Dim
                else -> Eye
            }
            val lift = when {
                mode == "thinking" -> -0.06f
                asleep -> 0.04f
                mode == "timer" || mode == "card" -> -0.14f
                else -> -0.05f
            }
            for (side in listOf(-1f, 1f)) {
                val cx = center.x + side * this.size.width * 0.13f
                val cy = center.y + this.size.height * lift
                if (!asleep && mode != "offline") {
                    for (k in 3 downTo 1) {   // мягкое свечение в три слоя
                        val g = k * this.size.width * 0.016f
                        drawRoundRect(EyeGlow.copy(alpha = 0.10f), Offset(cx - ew / 2 - g, cy - eh / 2 - g),
                                      Size(ew + 2 * g, eh + 2 * g), CornerRadius(ew * 0.45f + g))
                    }
                }
                drawRoundRect(eyeColor, Offset(cx - ew / 2, cy - eh / 2), Size(ew, eh), CornerRadius(ew * 0.45f))
            }
        }
        // подпись внутри экрана: часы, таймер или карточка — как на колонке
        val inner = Modifier.padding(horizontal = size * 0.18f)
        when (mode) {
            "idle", "player" -> Text(clock, color = Eye, fontSize = (size.value * 0.11f).sp, fontWeight = FontWeight.Medium,
                                     modifier = Modifier.align(Alignment.Center).padding(top = size * 0.38f))
            "sleep" -> Text(clock, color = Dim, fontSize = (size.value * 0.09f).sp,
                            modifier = Modifier.align(Alignment.TopCenter).padding(top = size * 0.16f))
            "timer" -> {
                val left = ((screen.until - now).coerceAtLeast(0) / 1000).toInt()
                Text("%d:%02d".format(left / 60, left % 60), color = Eye, fontSize = (size.value * 0.15f).sp,
                     fontWeight = FontWeight.SemiBold, modifier = Modifier.align(Alignment.Center).padding(top = size * 0.26f))
            }
            "card" -> Column(inner.align(Alignment.Center).padding(top = size * 0.3f), horizontalAlignment = Alignment.CenterHorizontally) {
                Text(screen.title, color = Dim, fontSize = (size.value * 0.06f).sp, maxLines = 1)
                Text(screen.detail, color = Eye, fontSize = (size.value * 0.08f).sp, fontWeight = FontWeight.SemiBold,
                     maxLines = 2, overflow = TextOverflow.Ellipsis, textAlign = TextAlign.Center)
            }
            else -> {}
        }
    }
}
