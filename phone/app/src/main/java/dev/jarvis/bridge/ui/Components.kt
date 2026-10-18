package dev.jarvis.bridge.ui

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.spring
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import kotlin.math.roundToInt

// Компоненты в духе iOS: карточка-секция (insetGrouped), строки высотой от 44 dp,
// переключатель, сегменты, степпер и слайдер как в системных «Настройках».

@Composable
fun Section(header: String? = null, footer: String? = null, content: @Composable ColumnScope.() -> Unit) {
    val c = LocalPalette.current
    Column(Modifier.fillMaxWidth().padding(top = 24.dp)) {
        if (header != null) {
            Text(header, style = Type.footnote, color = c.secondary, modifier = Modifier.padding(start = 16.dp, bottom = 7.dp))
        }
        Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(c.card)) { content() }
        if (footer != null) {
            Text(footer, style = Type.footnote, color = c.secondary, modifier = Modifier.padding(start = 16.dp, end = 16.dp, top = 7.dp))
        }
    }
}

/** Тонкая линия между строками, с отступом слева, как в iOS. */
@Composable
fun Separator(inset: Int = 16) {
    val c = LocalPalette.current
    Box(Modifier.fillMaxWidth().padding(start = inset.dp).height(0.5.dp).background(c.separator))
}

@Composable
fun ListRow(
    title: String,
    modifier: Modifier = Modifier,
    subtitle: String? = null,
    icon: ImageBitmap? = null,
    titleColor: Color? = null,
    onClick: (() -> Unit)? = null,
    trailing: @Composable (() -> Unit)? = null,
) {
    val c = LocalPalette.current
    Row(
        modifier
            .fillMaxWidth()
            .heightIn(min = 44.dp)
            .then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier)
            .padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        if (icon != null) {
            Image(icon, contentDescription = null, modifier = Modifier.size(29.dp).clip(RoundedCornerShape(7.dp)))
        }
        Column(Modifier.weight(1f)) {
            Text(title, style = Type.body, color = titleColor ?: c.label, maxLines = 2, overflow = TextOverflow.Ellipsis)
            if (subtitle != null) Text(subtitle, style = Type.footnote, color = c.secondary, maxLines = 2)
        }
        if (trailing != null) trailing()
    }
}

@Composable
fun ValueText(text: String, color: Color? = null) {
    Text(text, style = Type.body, color = color ?: LocalPalette.current.secondary)
}

/** Переключатель iOS: 51×31, зелёный, с пружинкой. */
@Composable
fun IosSwitch(checked: Boolean, label: String, onChange: (Boolean) -> Unit) {
    val c = LocalPalette.current
    val haptic = LocalHapticFeedback.current
    val t by animateFloatAsState(if (checked) 1f else 0f, spring(dampingRatio = 0.75f, stiffness = 600f), label = "switch")
    Box(
        Modifier
            .size(51.dp, 31.dp)
            .clip(CircleShape)
            .background(lerp(c.switchOff, c.green, t))
            .semantics {
                role = Role.Switch
                contentDescription = label
                stateDescription = if (checked) "включено" else "выключено"
            }
            .clickable(interactionSource = remember { MutableInteractionSource() }, indication = null) {
                haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                onChange(!checked)
            },
    ) {
        Box(
            Modifier
                .padding(2.dp)
                .offset(x = 20.dp * t)
                .size(27.dp)
                .shadow(3.dp, CircleShape)
                .background(Color.White, CircleShape)
        )
    }
}

@Composable
fun ToggleRow(title: String, checked: Boolean, subtitle: String? = null, onChange: (Boolean) -> Unit) {
    ListRow(title, subtitle = subtitle) { IosSwitch(checked, title, onChange) }
}

/** Сегменты (UISegmentedControl). */
@Composable
fun Segmented(options: List<String>, selected: Int, modifier: Modifier = Modifier, onSelect: (Int) -> Unit) {
    val c = LocalPalette.current
    Row(
        modifier.height(32.dp).clip(RoundedCornerShape(9.dp)).background(c.fill).padding(2.dp),
    ) {
        options.forEachIndexed { i, s ->
            val bg by animateColorAsState(if (i == selected) c.segment else Color.Transparent, label = "seg")
            Box(
                Modifier
                    .width(86.dp)
                    .fillMaxHeight()
                    .then(if (i == selected) Modifier.shadow(1.dp, RoundedCornerShape(7.dp)) else Modifier)
                    .clip(RoundedCornerShape(7.dp))
                    .background(bg)
                    .semantics { role = Role.RadioButton; contentDescription = s }
                    .clickable { onSelect(i) },
                contentAlignment = Alignment.Center,
            ) {
                Text(s, style = Type.footnote.copy(fontWeight = if (i == selected) androidx.compose.ui.text.font.FontWeight.SemiBold else androidx.compose.ui.text.font.FontWeight.Medium), color = c.label)
            }
        }
    }
}

/** Степпер (UIStepper): «−» и «+» в одной капсуле. */
@Composable
fun Stepper(label: String, onMinus: () -> Unit, onPlus: () -> Unit) {
    val c = LocalPalette.current
    val haptic = LocalHapticFeedback.current
    Row(Modifier.height(32.dp).clip(RoundedCornerShape(8.dp)).background(c.fill), verticalAlignment = Alignment.CenterVertically) {
        Box(
            Modifier.width(47.dp).fillMaxHeight().semantics { contentDescription = "$label: меньше" }
                .clickable { haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove); onMinus() },
            contentAlignment = Alignment.Center,
        ) { Text("−", style = Type.title3, color = c.label) }
        Box(Modifier.width(0.5.dp).height(18.dp).background(c.separator))
        Box(
            Modifier.width(47.dp).fillMaxHeight().semantics { contentDescription = "$label: больше" }
                .clickable { haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove); onPlus() },
            contentAlignment = Alignment.Center,
        ) { Text("+", style = Type.title3, color = c.label) }
    }
}

@Composable
fun StepperRow(title: String, value: String, onMinus: () -> Unit, onPlus: () -> Unit) {
    ListRow(title, subtitle = value) { Stepper(title, onMinus, onPlus) }
}

/** Слайдер iOS: тонкая дорожка, белый круглый бегунок. onDone — когда отпустили. */
@Composable
fun IosSlider(value: Float, label: String, onChange: (Float) -> Unit, onDone: () -> Unit) {
    val c = LocalPalette.current
    val change by rememberUpdatedState(onChange)
    val done by rememberUpdatedState(onDone)
    var width by remember { mutableFloatStateOf(1f) }
    val thumb = 28.dp
    val thumbPx = with(LocalDensity.current) { thumb.toPx() }
    fun at(x: Float) = ((x - thumbPx / 2) / (width - thumbPx).coerceAtLeast(1f)).coerceIn(0f, 1f)
    Box(
        Modifier
            .fillMaxWidth()
            .height(36.dp)
            .onSizeChanged { width = it.width.toFloat() }
            .semantics { contentDescription = label; stateDescription = "${(value * 100).roundToInt()} %" }
            .pointerInput(Unit) {
                detectHorizontalDragGestures(onDragEnd = { done() }) { ch, _ ->
                    ch.consume()
                    change(at(ch.position.x))
                }
            }
            .pointerInput(Unit) {
                detectTapGestures { change(at(it.x)); done() }
            },
        contentAlignment = Alignment.CenterStart,
    ) {
        val v = value.coerceIn(0f, 1f)
        Box(Modifier.fillMaxWidth().height(4.dp).clip(CircleShape).background(c.fill))
        Box(
            Modifier
                .fillMaxWidth(((thumbPx / 2 + (width - thumbPx) * v) / width).coerceIn(0f, 1f))
                .height(4.dp)
                .clip(CircleShape)
                .background(c.blue)
        )
        Box(
            Modifier
                .offset { IntOffset(((width - thumbPx) * v).roundToInt(), 0) }
                .size(thumb)
                .shadow(4.dp, CircleShape)
                .background(Color.White, CircleShape)
        )
    }
}

@Composable
fun SliderRow(title: String, value: Float, left: String, right: String, onChange: (Float) -> Unit, onDone: () -> Unit) {
    val c = LocalPalette.current
    Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 10.dp)) {
        Text(title, style = Type.body, color = c.label)
        Spacer(Modifier.height(4.dp))
        IosSlider(value, title, onChange, onDone)
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(left, style = Type.caption, color = c.secondary)
            Text(right, style = Type.caption, color = c.secondary)
        }
    }
}

/** Строка-кнопка: синий текст, как действия в «Настройках». */
@Composable
fun ActionRow(title: String, destructive: Boolean = false, onClick: () -> Unit) {
    val c = LocalPalette.current
    ListRow(title, titleColor = if (destructive) c.red else c.blue, onClick = onClick)
}
