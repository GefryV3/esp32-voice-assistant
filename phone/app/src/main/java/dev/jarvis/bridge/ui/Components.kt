@file:OptIn(ExperimentalMaterial3ExpressiveApi::class)

package dev.jarvis.bridge.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Check
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.ListItem
import androidx.compose.material3.ListItemDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.compositionLocalOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.getValue
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import kotlin.math.roundToInt

// Группы настроек как в Android 16: заголовок цветом primary, строки-карточки с зазором 2 dp,
// снаружи группа скруглена на 24 dp, между строками — 4 dp.

private val Outer = 24.dp
private val Inner = 4.dp

/** Положение строки в группе — от него зависят скругления. */
private val LocalRowShape = compositionLocalOf { RoundedCornerShape(Outer) }

class GroupScope internal constructor() {
    internal val rows = mutableListOf<@Composable () -> Unit>()

    /** Одна строка группы. */
    fun row(content: @Composable () -> Unit) {
        rows += content
    }
}

@Composable
fun Group(title: String? = null, footer: String? = null, build: GroupScope.() -> Unit) {
    val scope = GroupScope().apply(build)
    Column(Modifier.fillMaxWidth().padding(top = 20.dp)) {
        if (title != null) {
            Text(
                title, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary,
                modifier = Modifier.padding(start = 16.dp, bottom = 8.dp),
            )
        }
        Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
            val n = scope.rows.size
            scope.rows.forEachIndexed { i, row ->
                val top = if (i == 0) Outer else Inner
                val bottom = if (i == n - 1) Outer else Inner
                CompositionLocalProvider(
                    LocalRowShape provides RoundedCornerShape(top, top, bottom, bottom),
                ) { row() }
            }
        }
        if (footer != null) {
            Text(
                footer, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 16.dp, end = 16.dp, top = 8.dp),
            )
        }
    }
}

/** Ведущая иконка строки — в тональном кружке. */
@Composable
fun RowIcon(icon: ImageVector) {
    Box(
        Modifier.size(40.dp).background(MaterialTheme.colorScheme.secondaryContainer, CircleShape),
        contentAlignment = Alignment.Center,
    ) { Icon(icon, contentDescription = null, tint = MaterialTheme.colorScheme.onSecondaryContainer) }
}

@Composable
fun AppIcon(bitmap: ImageBitmap) {
    Image(bitmap, contentDescription = null, modifier = Modifier.size(40.dp).clip(CircleShape))
}

@Composable
fun SettingRow(
    title: String,
    subtitle: String? = null,
    leading: (@Composable () -> Unit)? = null,
    onClick: (() -> Unit)? = null,
    hapticOnClick: Boolean = true,
    trailing: (@Composable () -> Unit)? = null,
) {
    val shape = LocalRowShape.current
    val haptics = rememberHaptics()
    val colors = ListItemDefaults.colors(containerColor = MaterialTheme.colorScheme.row)
    val content: @Composable () -> Unit = {
        ListItem(
            headlineContent = { Text(title, maxLines = 2, overflow = TextOverflow.Ellipsis) },
            supportingContent = subtitle?.let { { Text(it, maxLines = 3, overflow = TextOverflow.Ellipsis) } },
            leadingContent = leading,
            trailingContent = trailing,
            colors = colors,
        )
    }
    if (onClick != null) {
        Surface(onClick = { if (hapticOnClick) haptics.click(); onClick() }, shape = shape, color = MaterialTheme.colorScheme.row, modifier = Modifier.fillMaxWidth()) { content() }
    } else {
        Surface(shape = shape, color = MaterialTheme.colorScheme.row, modifier = Modifier.fillMaxWidth()) { content() }
    }
}

@Composable
fun ToggleRow(title: String, checked: Boolean, subtitle: String? = null, leading: (@Composable () -> Unit)? = null, onChange: (Boolean) -> Unit) {
    val haptics = rememberHaptics()
    fun flip() {
        haptics.toggle(!checked)
        onChange(!checked)
    }
    SettingRow(title, subtitle, leading, onClick = ::flip, hapticOnClick = false) {
        Switch(
            checked = checked,
            onCheckedChange = { flip() },
            thumbContent = if (checked) {
                { Icon(Icons.Filled.Check, contentDescription = null, modifier = Modifier.size(SwitchDefaults.IconSize)) }
            } else null,
            modifier = Modifier.semantics { contentDescription = title },
        )
    }
}

/** Значение и пара кнопок «−» / «+». */
@Composable
fun StepperRow(title: String, value: String, leading: (@Composable () -> Unit)? = null, onMinus: () -> Unit, onPlus: () -> Unit) {
    val haptics = rememberHaptics()
    SettingRow(title, value, leading) {
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            FilledTonalIconButton(
                onClick = { haptics.step(); onMinus() },
                modifier = Modifier.semantics { contentDescription = "$title: меньше" },
            ) { Text("−", style = MaterialTheme.typography.titleLarge) }
            FilledTonalIconButton(
                onClick = { haptics.step(); onPlus() },
                modifier = Modifier.semantics { contentDescription = "$title: больше" },
            ) { Icon(Icons.Filled.Add, contentDescription = null) }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChoiceRow(title: String, options: List<String>, selected: Int, leading: (@Composable () -> Unit)? = null, onSelect: (Int) -> Unit) {
    val shape = LocalRowShape.current
    val haptics = rememberHaptics()
    Surface(shape = shape, color = MaterialTheme.colorScheme.row, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(start = 16.dp, end = 16.dp, top = 12.dp, bottom = 14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (leading != null) {
                    leading()
                    Spacer(Modifier.width(16.dp))
                }
                Text(title, style = MaterialTheme.typography.bodyLarge)
            }
            Spacer(Modifier.height(10.dp))
            SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                options.forEachIndexed { i, label ->
                    SegmentedButton(
                        selected = i == selected,
                        onClick = { if (i != selected) haptics.step(); onSelect(i) },
                        shape = SegmentedButtonDefaults.itemShape(i, options.size),
                    ) { Text(label) }
                }
            }
        }
    }
}

/** Ползунок: на экране меняется сразу, onDone — когда отпустили (тогда уходит на сервер). */
@Composable
fun SliderRow(
    title: String,
    value: Float,
    left: String,
    right: String,
    leading: (@Composable () -> Unit)? = null,
    valueText: String = "${(value * 100).roundToInt()} %",
    default: Float? = null,
    onChange: (Float) -> Unit,
    onDone: () -> Unit,
) {
    val shape = LocalRowShape.current
    val haptics = rememberHaptics()
    val sliderHaptics = remember(haptics) { SliderHaptics(haptics, default = default, initial = value) }
    Surface(shape = shape, color = MaterialTheme.colorScheme.row, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(start = 16.dp, end = 16.dp, top = 12.dp, bottom = 10.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (leading != null) {
                    leading()
                    Spacer(Modifier.width(16.dp))
                }
                Text(title, style = MaterialTheme.typography.bodyLarge, modifier = Modifier.weight(1f))
                Text(valueText, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary,
                     modifier = Modifier.widthIn(min = 40.dp))
            }
            Slider(
                value = value,
                onValueChange = {
                    sliderHaptics.onMove(it)
                    onChange(it)
                },
                onValueChangeFinished = { sliderHaptics.onRelease(); onDone() },
                modifier = Modifier.semantics { contentDescription = title; stateDescription = valueText },
            )
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text(left, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Text(right, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

/** Маленький цветной кружок состояния. */
@Composable
fun Dot(color: androidx.compose.ui.graphics.Color, size: Dp = 10.dp) {
    Box(Modifier.size(size).background(color, CircleShape))
}

/** Тональная кнопка с откликом. */
@Composable
fun HapticTonalButton(onClick: () -> Unit, content: @Composable () -> Unit) {
    val haptics = rememberHaptics()
    androidx.compose.material3.FilledTonalButton(onClick = { haptics.click(); onClick() }) { content() }
}
