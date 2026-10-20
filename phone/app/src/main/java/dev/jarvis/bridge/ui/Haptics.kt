package dev.jarvis.bridge.ui

import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.hapticfeedback.HapticFeedback
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalHapticFeedback
import dev.jarvis.bridge.Prefs

/**
 * Тактильный отклик приложения в одном месте. Выключается переключателем «Виброотклик»
 * (Prefs.haptics); системная настройка «Отклик при касании» учитывается самим Android:
 * performHapticFeedback у View её соблюдает.
 */
class Haptics internal constructor(private val hf: HapticFeedback) {
    private fun go(type: HapticFeedbackType) {
        if (Prefs.haptics) hf.performHapticFeedback(type)
    }

    /** Шаг ползунка. */
    fun tick() = go(HapticFeedbackType.SegmentFrequentTick)

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
    return remember(hf) { Haptics(hf) }
}
