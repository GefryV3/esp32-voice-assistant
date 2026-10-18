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
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBars
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.core.graphics.drawable.toBitmap
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import dev.jarvis.bridge.ui.ActionRow
import dev.jarvis.bridge.ui.AppTheme
import dev.jarvis.bridge.ui.IosSwitch
import dev.jarvis.bridge.ui.ListRow
import dev.jarvis.bridge.ui.LocalPalette
import dev.jarvis.bridge.ui.Section
import dev.jarvis.bridge.ui.Segmented
import dev.jarvis.bridge.ui.Separator
import dev.jarvis.bridge.ui.SliderRow
import dev.jarvis.bridge.ui.StepperRow
import dev.jarvis.bridge.ui.ToggleRow
import dev.jarvis.bridge.ui.Type
import dev.jarvis.bridge.ui.ValueText
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

@Composable
private fun Home() {
    val ctx = LocalContext.current
    val c = LocalPalette.current
    val state by Bridge.ui.collectAsStateWithLifecycle()
    val s = state.settings
    var perms by remember { mutableStateOf(Permissions.check(ctx)) }
    var editServer by remember { mutableStateOf(false) }
    // ползунки меняют значение сразу на экране, а серверу — когда палец отпустили
    var brightness by remember(s.brightness) { mutableStateOf(s.brightness / 100f) }
    var sarcasm by remember(s.sarcasm) { mutableStateOf(s.sarcasm / 100f) }

    LifecycleResumeEffect(Unit) {
        perms = Permissions.check(ctx)
        onPauseOrDispose { }
    }
    val askRuntime = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        perms = Permissions.check(ctx)
        Calls.register(ctx)
        Bt.sendAll(ctx)
    }

    Column(
        Modifier
            .fillMaxSize()
            .background(c.bg)
            .verticalScroll(rememberScrollState())
            .windowInsetsPadding(WindowInsets.systemBars)
            .padding(horizontal = 16.dp)
            .padding(bottom = 32.dp)
    ) {
        Hero(state)

        if (!perms.all) {
            Section(
                "Доступы",
                footer = "Если Android пишет «Ограниченная настройка»: О приложении → ⋮ → Разрешить ограниченные настройки, потом снова сюда.",
            ) {
                PermissionRow("Уведомления", "Видеть уведомления и что играет", perms.listener) {
                    ctx.startActivity(
                        Intent(Settings.ACTION_NOTIFICATION_LISTENER_DETAIL_SETTINGS)
                            .putExtra(
                                Settings.EXTRA_NOTIFICATION_LISTENER_COMPONENT_NAME,
                                android.content.ComponentName(ctx, NotifListener::class.java).flattenToString(),
                            )
                    )
                }
                Separator()
                PermissionRow("Звонки, SMS, Bluetooth", "Кто звонит, «сбрось», заряд наушников", perms.runtime) {
                    askRuntime.launch(Permissions.runtime)
                }
                Separator()
                PermissionRow("Работа в фоне", "Чтобы OnePlus не выгружал приложение", perms.battery) {
                    ctx.startActivity(
                        Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${ctx.packageName}"))
                    )
                }
            }
        }

        Section("Экран") {
            StepperRow(
                "Засыпать в тишине через", "${s.sleepAfterMin} мин",
                onMinus = { Bridge.updateSettings { it.copy(sleepAfterMin = (it.sleepAfterMin - 1).coerceAtLeast(1)) } },
                onPlus = { Bridge.updateSettings { it.copy(sleepAfterMin = (it.sleepAfterMin + 1).coerceAtMost(60)) } },
            )
            Separator()
            ListRow("Часы во сне") {
                Segmented(listOf("Сверху", "Снизу"), if (s.sleepClock == "bottom") 1 else 0) { i ->
                    Bridge.updateSettings { it.copy(sleepClock = if (i == 1) "bottom" else "top") }
                }
            }
            Separator()
            SliderRow(
                "Яркость", brightness, "тусклее", "ярче",
                onChange = { brightness = it.coerceAtLeast(0.05f) },
                onDone = { Bridge.updateSettings { it.copy(brightness = (brightness * 100).toInt()) } },
            )
        }

        Section("Ночь", footer = "В тишину колонка гаснет и показывает только звонки и будильник.") {
            StepperRow(
                "Тишина с", s.nightStart,
                onMinus = { Bridge.updateSettings { it.copy(nightStart = shiftTime(it.nightStart, -30)) } },
                onPlus = { Bridge.updateSettings { it.copy(nightStart = shiftTime(it.nightStart, 30)) } },
            )
            Separator()
            StepperRow(
                "Тишина до", s.nightEnd,
                onMinus = { Bridge.updateSettings { it.copy(nightEnd = shiftTime(it.nightEnd, -30)) } },
                onPlus = { Bridge.updateSettings { it.copy(nightEnd = shiftTime(it.nightEnd, 30)) } },
            )
            Separator()
            ToggleRow("Проверка перед сном", s.bedtimeCheck, subtitle = "Будильник, заряд, погода на завтра") { on ->
                Bridge.updateSettings { it.copy(bedtimeCheck = on) }
            }
            if (s.bedtimeCheck) {
                Separator()
                StepperRow(
                    "Показать в", s.bedtimeAt,
                    onMinus = { Bridge.updateSettings { it.copy(bedtimeAt = shiftTime(it.bedtimeAt, -15)) } },
                    onPlus = { Bridge.updateSettings { it.copy(bedtimeAt = shiftTime(it.bedtimeAt, 15)) } },
                )
            }
            Separator()
            StepperRow(
                "Разбудить, если уснул после будильника", "через ${s.alarmWatchdogMin} мин",
                onMinus = { Bridge.updateSettings { it.copy(alarmWatchdogMin = (it.alarmWatchdogMin - 1).coerceAtLeast(1)) } },
                onPlus = { Bridge.updateSettings { it.copy(alarmWatchdogMin = (it.alarmWatchdogMin + 1).coerceAtMost(30)) } },
            )
        }

        Section("Характер") {
            SliderRow(
                "Язвительность: ${sarcasmLabel((sarcasm * 100).toInt())}", sarcasm, "вежливый", "токсичный",
                onChange = { sarcasm = it },
                onDone = { Bridge.updateSettings { it.copy(sarcasm = (sarcasm * 100).toInt()) } },
            )
        }

        Section(
            "Уведомления",
            footer = "Приложения появляются в списке, когда пришлют первое уведомление.",
        ) {
            ToggleRow("Показывать на колонке", s.mirror) { on -> Bridge.updateSettings { it.copy(mirror = on) } }
            if (s.mirror) {
                Separator()
                ToggleRow(
                    "Только когда я за компьютером", s.mirrorOnlyPc,
                    subtitle = if (state.pcOnline) "Компьютер на связи" else "Компьютер не на связи",
                ) { on -> Bridge.updateSettings { it.copy(mirrorOnlyPc = on) } }
                AppList(state.seenApps, state.muted)
            }
        }

        Section("Проверка") {
            ActionRow("Показать уведомление на колонке") { Bridge.send("test") }
            Separator()
            ActionRow("Позвонить на этот телефон") { Finder.ring(ctx, true) }
        }

        Section("Связь", footer = "Версия ${BuildConfig.VERSION_NAME}") {
            ListRow("Сервер", subtitle = Prefs.serverUrl, onClick = { editServer = true }) {
                ValueText(if (state.link) "на связи" else "нет связи", if (state.link) c.green else c.red)
            }
        }
    }

    if (editServer) ServerDialog { editServer = false }
}

@Composable
private fun PermissionRow(title: String, subtitle: String, ok: Boolean, onClick: () -> Unit) {
    val c = LocalPalette.current
    ListRow(title, subtitle = subtitle, onClick = if (ok) null else onClick) {
        ValueText(if (ok) "Есть" else "Разрешить", if (ok) c.green else c.blue)
    }
}

@Composable
private fun AppList(seen: Set<String>, muted: Set<String>) {
    val ctx = LocalContext.current
    val pm = ctx.packageManager
    val apps = remember(seen) {
        seen.mapNotNull { pkg ->
            try {
                val info = pm.getApplicationInfo(pkg, 0)
                Triple(pkg, pm.getApplicationLabel(info).toString(), pm.getApplicationIcon(info).toBitmap(96, 96).asImageBitmap())
            } catch (e: Exception) {
                null
            }
        }.sortedBy { it.second.lowercase() }
    }
    apps.forEach { (pkg, name, icon) ->
        Separator(inset = 57)
        ListRow(name, icon = icon) {
            IosSwitch(pkg !in muted, name) { on -> Bridge.setMuted(pkg, !on) }
        }
    }
}

/** Вверху — сама колонка: круглый экран с глазами, которые моргают. */
@Composable
private fun Hero(state: UiState) {
    val c = LocalPalette.current
    val blink = remember { Animatable(1f) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(2500L + Random.nextLong(3500))
            blink.animateTo(0.08f, tween(70))
            blink.animateTo(1f, tween(150))
        }
    }
    val online = state.link && state.speakerOnline
    val eye = if (online) Color(0xFFE2ECFF) else Color(0xFF8E939E)
    val glow = if (online) Color(0xFF4080FF) else Color(0xFF4A4F5A)
    Column(
        Modifier.fillMaxWidth().padding(top = 28.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Canvas(Modifier.size(148.dp).semantics { contentDescription = if (online) "Колонка на связи" else "Колонка не на связи" }) {
            val r = size.minDimension / 2
            drawCircle(Brush.radialGradient(listOf(Color(0xFF3A3F48), Color(0xFF0B0C0E)), center = Offset(r * 0.6f, r * 0.45f), radius = r * 1.4f), r)
            drawCircle(Brush.radialGradient(listOf(Color(0xFF151922), Color.Black), center = center, radius = r * 0.9f), r * 0.9f)
            val ew = size.width * 0.17f
            val eh = size.height * 0.22f * blink.value
            for (side in listOf(-1f, 1f)) {
                val cx = center.x + side * size.width * 0.155f
                val cy = center.y - size.height * 0.03f
                for (k in 3 downTo 1) {  // мягкое свечение в три слоя
                    val g = k * size.width * 0.018f
                    drawRoundRect(
                        glow.copy(alpha = 0.10f), Offset(cx - ew / 2 - g, cy - eh / 2 - g), Size(ew + 2 * g, eh + 2 * g),
                        CornerRadius(ew * 0.4f + g),
                    )
                }
                drawRoundRect(
                    Brush.verticalGradient(listOf(eye, eye.copy(alpha = 0.82f)), startY = cy - eh / 2, endY = cy + eh / 2),
                    Offset(cx - ew / 2, cy - eh / 2), Size(ew, eh), CornerRadius(ew * 0.38f),
                )
            }
        }
        Spacer(Modifier.height(14.dp))
        Text("Колонка", style = Type.largeTitle, color = c.label)
        Spacer(Modifier.height(4.dp))
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            Box(Modifier.size(8.dp).background(if (online) c.green else if (state.link) c.orange else c.red, CircleShape))
            Text(
                when {
                    online -> "На связи · ${state.speakerFps} кадров/с"
                    state.link -> "Сервер на связи, колонка — нет"
                    else -> "Нет связи с сервером"
                },
                style = Type.subhead, color = c.secondary,
            )
        }
    }
}

@Composable
private fun ServerDialog(onClose: () -> Unit) {
    var url by remember { mutableStateOf(Prefs.serverUrl) }
    AlertDialog(
        onDismissRequest = onClose,
        title = { Text("Адрес сервера", style = Type.bodyStrong) },
        text = {
            OutlinedTextField(
                url, { url = it }, singleLine = true, textStyle = Type.subhead,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
            )
        },
        confirmButton = {
            TextButton(onClick = {
                Prefs.serverUrl = url.trim()
                Bridge.restartLink()
                onClose()
            }) { Text("Сохранить", style = Type.bodyStrong) }
        },
        dismissButton = { TextButton(onClick = onClose) { Text("Отмена", style = Type.body) } },
    )
}
