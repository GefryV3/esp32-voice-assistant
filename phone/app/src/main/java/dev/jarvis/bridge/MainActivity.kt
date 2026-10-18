@file:OptIn(ExperimentalMaterial3Api::class, ExperimentalMaterial3ExpressiveApi::class)

package dev.jarvis.bridge

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material.icons.filled.Done
import androidx.compose.material.icons.filled.Face
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material.icons.filled.Phone
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Send
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Star
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.LargeFlexibleTopAppBar
import androidx.compose.material3.MaterialShapes
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.toShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.input.nestedscroll.nestedScroll
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.core.graphics.drawable.toBitmap
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import dev.jarvis.bridge.ui.AppIcon
import dev.jarvis.bridge.ui.AppTheme
import dev.jarvis.bridge.ui.ChoiceRow
import dev.jarvis.bridge.ui.Dot
import dev.jarvis.bridge.ui.Group
import dev.jarvis.bridge.ui.RowIcon
import dev.jarvis.bridge.ui.SettingRow
import dev.jarvis.bridge.ui.SliderRow
import dev.jarvis.bridge.ui.Status
import dev.jarvis.bridge.ui.StepperRow
import dev.jarvis.bridge.ui.ToggleRow
import dev.jarvis.bridge.ui.screen
import kotlinx.coroutines.delay
import kotlin.random.Random

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        BridgeService.start(this)
        setContent { AppTheme { Home() } }
    }

    override fun onResume() {
        super.onResume()
        Finder.stop(this)  // открыл приложение — значит, телефон нашёлся
    }
}

/** Время «чч:мм» ± шаг в минутах, по кругу через полночь. */
private fun shiftTime(hhmm: String, minutes: Int): String {
    val (h, m) = hhmm.split(":").map { it.toIntOrNull() ?: 0 }
    val t = ((h * 60 + m + minutes) % 1440 + 1440) % 1440
    return "%02d:%02d".format(t / 60, t % 60)
}

private fun sarcasmLabel(v: Int) = when {
    v < 20 -> "вежливый"
    v < 45 -> "с юмором"
    v < 70 -> "колкий"
    v < 90 -> "язвительный"
    else -> "токсичный"
}

private fun volumeLabel(v: Int) = when {
    v == 0 -> "без звука"
    v < 35 -> "тихо"
    v < 70 -> "${v} %"
    else -> "${v} % · громко"
}

private data class SeenApp(val pkg: String, val name: String, val icon: androidx.compose.ui.graphics.ImageBitmap)

@Composable
private fun Home() {
    val ctx = LocalContext.current
    val state by Bridge.ui.collectAsStateWithLifecycle()
    val s = state.settings
    var perms by remember { mutableStateOf(Permissions.check(ctx)) }
    var editServer by remember { mutableStateOf(false) }
    // ползунки меняют значение сразу на экране, а серверу — когда палец отпустили
    var brightness by remember(s.brightness) { mutableStateOf(s.brightness / 100f) }
    var volume by remember(s.volume) { mutableStateOf(s.volume / 100f) }
    var sarcasm by remember(s.sarcasm) { mutableStateOf(s.sarcasm / 100f) }
    val scroll = TopAppBarDefaults.exitUntilCollapsedScrollBehavior()

    LifecycleResumeEffect(Unit) {
        perms = Permissions.check(ctx)
        onPauseOrDispose { }
    }
    val askRuntime = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        perms = Permissions.check(ctx)
        Calls.register(ctx)
        Bt.sendAll(ctx)
    }
    val pm = ctx.packageManager
    val apps = remember(state.seenApps) {
        state.seenApps.mapNotNull { pkg ->
            try {
                val info = pm.getApplicationInfo(pkg, 0)
                SeenApp(pkg, pm.getApplicationLabel(info).toString(), pm.getApplicationIcon(info).toBitmap(120, 120).asImageBitmap())
            } catch (e: Exception) {
                null
            }
        }.sortedBy { it.name.lowercase() }
    }

    Scaffold(
        modifier = Modifier.nestedScroll(scroll.nestedScrollConnection),
        containerColor = MaterialTheme.colorScheme.screen,
        topBar = {
            LargeFlexibleTopAppBar(
                title = { Text("Колонка") },
                subtitle = { Text(statusText(state)) },
                scrollBehavior = scroll,
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.screen,
                    scrolledContainerColor = MaterialTheme.colorScheme.surfaceContainerHigh,
                ),
            )
        },
    ) { inner ->
        LazyColumn(
            contentPadding = PaddingValues(
                start = 16.dp, end = 16.dp, top = inner.calculateTopPadding(), bottom = inner.calculateBottomPadding() + 32.dp,
            ),
            modifier = Modifier.fillMaxSize(),
        ) {
            item { Hero(state) }

            if (!perms.all) item {
                Group(
                    "Доступы",
                    footer = "Если Android пишет «Ограниченная настройка»: О приложении → ⋮ → Разрешить ограниченные настройки, потом снова сюда.",
                ) {
                    row {
                        PermissionRow(Icons.Filled.Notifications, "Уведомления", "Видеть уведомления и что играет", perms.listener) {
                            ctx.startActivity(
                                Intent(Settings.ACTION_NOTIFICATION_LISTENER_DETAIL_SETTINGS).putExtra(
                                    Settings.EXTRA_NOTIFICATION_LISTENER_COMPONENT_NAME,
                                    android.content.ComponentName(ctx, NotifListener::class.java).flattenToString(),
                                )
                            )
                        }
                    }
                    row {
                        PermissionRow(Icons.Filled.Phone, "Звонки, SMS, Bluetooth", "Кто звонит, «сбрось», заряд наушников", perms.runtime) {
                            askRuntime.launch(Permissions.runtime)
                        }
                    }
                    row {
                        PermissionRow(Icons.Filled.Lock, "Работа в фоне", "Чтобы OnePlus не выгружал приложение", perms.battery) {
                            ctx.startActivity(
                                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${ctx.packageName}"))
                            )
                        }
                    }
                }
            }

            item {
                Group("Звук") {
                    row {
                        SliderRow(
                            "Громкость", volume, "тише", "громче",
                            leading = { RowIcon(Icons.Filled.PlayArrow) },
                            valueText = volumeLabel((volume * 100).toInt()),
                            onChange = { volume = it },
                            onDone = { Bridge.updateSettings { it.copy(volume = (volume * 100).toInt()) } },
                        )
                    }
                    row {
                        SettingRow("Проверить звук", "Короткий сигнал на колонке", leading = { RowIcon(Icons.Filled.Refresh) }) {
                            FilledTonalButton(onClick = { Bridge.send("beep") }) { Text("Сигнал") }
                        }
                    }
                }
            }

            item {
                Group("Экран") {
                    row {
                        SliderRow(
                            "Яркость", brightness, "тусклее", "ярче",
                            leading = { RowIcon(Icons.Filled.Star) },
                            onChange = { brightness = it.coerceAtLeast(0.05f) },
                            onDone = { Bridge.updateSettings { it.copy(brightness = (brightness * 100).toInt()) } },
                        )
                    }
                    row {
                        StepperRow(
                            "Засыпать в тишине через", "${s.sleepAfterMin} мин", leading = { RowIcon(Icons.Filled.DateRange) },
                            onMinus = { Bridge.updateSettings { it.copy(sleepAfterMin = (it.sleepAfterMin - 1).coerceAtLeast(1)) } },
                            onPlus = { Bridge.updateSettings { it.copy(sleepAfterMin = (it.sleepAfterMin + 1).coerceAtMost(60)) } },
                        )
                    }
                    row {
                        ChoiceRow("Часы во сне", listOf("Сверху", "Снизу"), if (s.sleepClock == "bottom") 1 else 0) { i ->
                            Bridge.updateSettings { it.copy(sleepClock = if (i == 1) "bottom" else "top") }
                        }
                    }
                }
            }

            item {
                Group("Ночь", footer = "В тишину колонка гаснет и показывает только звонки и будильник.") {
                    row {
                        StepperRow(
                            "Тишина с", s.nightStart,
                            onMinus = { Bridge.updateSettings { it.copy(nightStart = shiftTime(it.nightStart, -30)) } },
                            onPlus = { Bridge.updateSettings { it.copy(nightStart = shiftTime(it.nightStart, 30)) } },
                        )
                    }
                    row {
                        StepperRow(
                            "Тишина до", s.nightEnd,
                            onMinus = { Bridge.updateSettings { it.copy(nightEnd = shiftTime(it.nightEnd, -30)) } },
                            onPlus = { Bridge.updateSettings { it.copy(nightEnd = shiftTime(it.nightEnd, 30)) } },
                        )
                    }
                    row {
                        ToggleRow(
                            "Проверка перед сном", s.bedtimeCheck, subtitle = "Будильник, заряд, погода на завтра",
                            leading = { RowIcon(Icons.Filled.Done) },
                        ) { on -> Bridge.updateSettings { it.copy(bedtimeCheck = on) } }
                    }
                    if (s.bedtimeCheck) row {
                        StepperRow(
                            "Показать в", s.bedtimeAt,
                            onMinus = { Bridge.updateSettings { it.copy(bedtimeAt = shiftTime(it.bedtimeAt, -15)) } },
                            onPlus = { Bridge.updateSettings { it.copy(bedtimeAt = shiftTime(it.bedtimeAt, 15)) } },
                        )
                    }
                    row {
                        StepperRow(
                            "Разбудить, если уснул после будильника", "через ${s.alarmWatchdogMin} мин",
                            leading = { RowIcon(Icons.Filled.Warning) },
                            onMinus = { Bridge.updateSettings { it.copy(alarmWatchdogMin = (it.alarmWatchdogMin - 1).coerceAtLeast(1)) } },
                            onPlus = { Bridge.updateSettings { it.copy(alarmWatchdogMin = (it.alarmWatchdogMin + 1).coerceAtMost(30)) } },
                        )
                    }
                }
            }

            item {
                Group("Характер") {
                    row {
                        SliderRow(
                            "Язвительность", sarcasm, "вежливый", "токсичный",
                            leading = { RowIcon(Icons.Filled.Face) },
                            valueText = sarcasmLabel((sarcasm * 100).toInt()),
                            onChange = { sarcasm = it },
                            onDone = { Bridge.updateSettings { it.copy(sarcasm = (sarcasm * 100).toInt()) } },
                        )
                    }
                }
            }

            item {
                Group("Уведомления", footer = "Приложения появляются в списке, когда пришлют первое уведомление.") {
                    row {
                        ToggleRow("Показывать на колонке", s.mirror, leading = { RowIcon(Icons.Filled.Notifications) }) { on ->
                            Bridge.updateSettings { it.copy(mirror = on) }
                        }
                    }
                    if (s.mirror) {
                        row {
                            ToggleRow(
                                "Только когда я за компьютером", s.mirrorOnlyPc,
                                subtitle = if (state.pcOnline) "Компьютер на связи" else "Компьютер не на связи",
                                leading = { RowIcon(Icons.Filled.Home) },
                            ) { on -> Bridge.updateSettings { it.copy(mirrorOnlyPc = on) } }
                        }
                        apps.forEach { app ->
                            row {
                                ToggleRow(app.name, app.pkg !in state.muted, leading = { AppIcon(app.icon) }) { on ->
                                    Bridge.setMuted(app.pkg, !on)
                                }
                            }
                        }
                    }
                }
            }

            item {
                Group("Проверка") {
                    row {
                        SettingRow("Уведомление на колонке", "Тестовая карточка «Мама: ужин готов»", leading = { RowIcon(Icons.Filled.Send) },
                                   onClick = { Bridge.send("test") })
                    }
                    row {
                        SettingRow("Позвонить на этот телефон", "Как «где мой телефон»", leading = { RowIcon(Icons.Filled.Call) },
                                   onClick = { Finder.ring(ctx, true) })
                    }
                }
            }

            item {
                Group("Связь", footer = "Версия ${BuildConfig.VERSION_NAME}") {
                    row {
                        SettingRow("Сервер", Prefs.serverUrl, leading = { RowIcon(Icons.Filled.Settings) }, onClick = { editServer = true }) {
                            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                                Dot(if (state.link) Status.ok else Status.bad)
                                Text(if (state.link) "на связи" else "нет связи", style = MaterialTheme.typography.labelLarge)
                            }
                        }
                    }
                }
            }
        }
    }

    if (editServer) ServerDialog { editServer = false }
}

private fun statusText(state: UiState) = when {
    state.link && state.speakerOnline -> "${state.screen.title} · ${state.speakerFps} кадров/с"
    state.link -> "Сервер на связи, колонка — нет"
    else -> "Нет связи с сервером"
}

@Composable
private fun PermissionRow(icon: androidx.compose.ui.graphics.vector.ImageVector, title: String, subtitle: String, ok: Boolean, onClick: () -> Unit) {
    SettingRow(title, subtitle, leading = { RowIcon(icon) }, onClick = if (ok) null else onClick) {
        if (ok) Text("Есть", style = MaterialTheme.typography.labelLarge, color = Status.ok)
        else FilledTonalButton(onClick = onClick) { Text("Разрешить") }
    }
}

/** Вверху — сама колонка в том состоянии, в каком она сейчас, и что она делает словами. */
@Composable
private fun Hero(state: UiState) {
    val screen = if (state.link && state.speakerOnline) state.screen else ScreenState()
    Column(Modifier.fillMaxWidth().padding(top = 8.dp, bottom = 4.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        SpeakerFace(screen)
        Spacer(Modifier.height(14.dp))
        Surface(shape = MaterialTheme.shapes.extraLarge, color = MaterialTheme.colorScheme.secondaryContainer) {
            Row(
                Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Dot(if (screen.mode == "offline") Status.bad else Status.ok)
                Text(screen.title, style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onSecondaryContainer)
                if (screen.detail.isNotBlank()) {
                    Text(screen.detail, style = MaterialTheme.typography.bodyMedium, maxLines = 1,
                         color = MaterialTheme.colorScheme.onSecondaryContainer.copy(alpha = 0.75f))
                }
            }
        }
    }
}

@Composable
private fun ServerDialog(onClose: () -> Unit) {
    var url by remember { mutableStateOf(Prefs.serverUrl) }
    AlertDialog(
        onDismissRequest = onClose,
        title = { Text("Адрес сервера") },
        text = {
            OutlinedTextField(
                url, { url = it }, singleLine = true, label = { Text("ws://…:8765/phone") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                modifier = Modifier.fillMaxWidth(),
            )
        },
        confirmButton = {
            TextButton(onClick = {
                Prefs.serverUrl = url.trim()
                Bridge.restartLink()
                onClose()
            }) { Text("Сохранить") }
        },
        dismissButton = { TextButton(onClick = onClose) { Text("Отмена") } },
    )
}
