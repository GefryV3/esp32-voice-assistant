package dev.jarvis.bridge.ui

import android.os.Build
import android.os.SystemClock
import android.view.HapticFeedbackConstants
import android.view.View
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.hapticfeedback.HapticFeedback
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.platform.LocalView
import dev.jarvis.bridge.Prefs
import kotlin.math.roundToInt

/**
 * Тактильный отклик приложения в одном месте. Выключается переключателем «Виброотклик»
 * (Prefs.haptics); системная настройка «Отклик при касании» учитывается самим Android:
 * performHapticFeedback у View её соблюдает.
 */
class Haptics internal constructor(private val hf: HapticFeedback, private val view: View) {
    private fun go(type: HapticFeedbackType) {
        if (Prefs.haptics) hf.performHapticFeedback(type)
    }

    private fun goView(constant: Int) {
        if (Prefs.haptics) view.performHapticFeedback(constant)
    }

    /** Шаг ползунка: SEGMENT_TICK заметнее, чем SEGMENT_FREQUENT_TICK (тот рассчитан на очень частые события). */
    fun tick() = goView(
        if (Build.VERSION.SDK_INT >= 34) HapticFeedbackConstants.SEGMENT_TICK else HapticFeedbackConstants.CLOCK_TICK,
    )

    /** Упор или значение по умолчанию на ползунке: чуть сильнее тика. */
    fun detent() = goView(
        if (Build.VERSION.SDK_INT >= 34) HapticFeedbackConstants.GESTURE_THRESHOLD_ACTIVATE else HapticFeedbackConstants.CONFIRM,
    )

    /** Выбор в сегментах, «−»/«+». */
    fun step() = go(HapticFeedbackType.SegmentTick)

    /** Ползунок отпущен, действие выполнено. */
    fun confirm() = go(HapticFeedbackType.Confirm)

    /** Обычное нажатие кнопки или плитки. */
    fun click() = go(HapticFeedbackType.ContextClick)

    fun toggle(on: Boolean) = go(if (on) HapticFeedbackType.ToggleOn else HapticFeedbackType.ToggleOff)

    /** Отказ или ошибка. */
    fun reject() = go(HapticFeedbackType.Reject)
}

@Composable
fun rememberHaptics(): Haptics {
    val hf = LocalHapticFeedback.current
    val view = LocalView.current
    return remember(hf, view) { Haptics(hf, view) }
}

/**
 * Отклик одного ползунка по рекомендациям Android (developer.android.com/develop/ui/views/haptics):
 * редкие мягкие тики на шагах (не чаще [minGapMs] и только при смене шага, иначе «жужжание»),
 * упор на краях и на значении по умолчанию заметнее, подтверждение при отпускании.
 */
class SliderHaptics(
    private val h: Haptics,
    private val steps: Int = 20,
    private val default: Float? = null,
    private val minGapMs: Long = 40,
    initial: Float,
) {
    private var lastStep = stepOf(initial)
    private var lastZone = zoneOf(lastStep)
    private var lastAt = 0L

    private fun stepOf(v: Float) = (v * steps).roundToInt().coerceIn(0, steps)

    /** 0 — низ, 1 — верх, 2 — значение по умолчанию, -1 — обычное место. */
    private fun zoneOf(step: Int) = when {
        step == 0 -> 0
        step == steps -> 1
        default != null && step == stepOf(default) -> 2
        else -> -1
    }

    fun onMove(v: Float) {
        val step = stepOf(v)
        if (step == lastStep) return
        lastStep = step
        val zone = zoneOf(step)
        val now = SystemClock.uptimeMillis()
        if (zone != -1 && zone != lastZone) {
            h.detent()
            lastAt = now
        } else if (now - lastAt >= minGapMs) {
            h.tick()
            lastAt = now
        }
        lastZone = zone
    }

    fun onRelease() = h.confirm()
}
