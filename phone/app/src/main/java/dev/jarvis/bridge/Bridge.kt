package dev.jarvis.bridge

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.RemoteInput
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.service.notification.StatusBarNotification
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.ConcurrentHashMap

/** Настройки колонки. Хранит их сервер, приложение показывает и меняет. */
data class SpeakerSettings(
    val sleepAfterMin: Int = 3,
    val sleepClock: String = "top",
    val brightness: Int = 100,
    val nightStart: String = "23:30",
    val nightEnd: String = "07:00",
    val bedtimeCheck: Boolean = true,
    val bedtimeAt: String = "23:00",
    val sarcasm: Int = 60,
    val mirror: Boolean = true,
    val mirrorOnlyPc: Boolean = true,
    val alarmWatchdogMin: Int = 5,
    val volume: Int = 50,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("sleep_after_min", sleepAfterMin)
        .put("sleep_clock", sleepClock)
        .put("brightness", brightness)
        .put("night_start", nightStart)
        .put("night_end", nightEnd)
        .put("bedtime_check", bedtimeCheck)
        .put("bedtime_at", bedtimeAt)
        .put("sarcasm", sarcasm)
        .put("mirror", mirror)
        .put("mirror_only_pc", mirrorOnlyPc)
        .put("alarm_watchdog_min", alarmWatchdogMin)
        .put("volume", volume)

    companion object {
        fun from(j: JSONObject, b: SpeakerSettings = SpeakerSettings()) = SpeakerSettings(
            sleepAfterMin = j.optInt("sleep_after_min", b.sleepAfterMin),
            sleepClock = j.optString("sleep_clock", b.sleepClock),
            brightness = j.optInt("brightness", b.brightness),
            nightStart = j.optString("night_start", b.nightStart),
            nightEnd = j.optString("night_end", b.nightEnd),
            bedtimeCheck = j.optBoolean("bedtime_check", b.bedtimeCheck),
            bedtimeAt = j.optString("bedtime_at", b.bedtimeAt),
            sarcasm = j.optInt("sarcasm", b.sarcasm),
            mirror = j.optBoolean("mirror", b.mirror),
            mirrorOnlyPc = j.optBoolean("mirror_only_pc", b.mirrorOnlyPc),
            alarmWatchdogMin = j.optInt("alarm_watchdog_min", b.alarmWatchdogMin),
            volume = j.optInt("volume", b.volume),
        )
    }
}

data class UiState(
    val link: Boolean = false,
    val speakerOnline: Boolean = false,
    val speakerFps: Int = 0,
    val pcOnline: Boolean = false,
    val settings: SpeakerSettings = SpeakerSettings(),
    val seenApps: Set<String> = emptySet(),
    val muted: Set<String> = emptySet(),
)

/**
 * Мост телефон ↔ сервер. Живёт всё время, пока жив процесс приложения: его запускает
 * система (служба уведомлений), загрузка телефона или открытие приложения.
 */
object Bridge {
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    val ui = MutableStateFlow(UiState())

    private lateinit var app: Context
    private var link: Link? = null
    private val active = ConcurrentHashMap<String, StatusBarNotification>()
    private val lastSent = ConcurrentHashMap<String, Int>()
    private val appNames = ConcurrentHashMap<String, String>()

    @Volatile
    private var ringingAlarmKey: String? = null

    @Volatile
    var callerName: String = ""
        private set

    @Synchronized
    fun start(ctx: Context) {
        if (link != null) return
        app = ctx.applicationContext
        ui.update { it.copy(seenApps = Prefs.seenApps, muted = Prefs.mutedApps) }
        link = Link(scope, ::onServerMessage, ::onLinkState).also { it.start() }
        PhoneState.register(app)
        Calls.register(app)
        Bt.register(app)
    }

    fun restartLink() = link?.restart()

    /** Отправить сообщение серверу; false — сейчас нет связи. */
    fun send(type: String, fill: JSONObject.() -> Unit = {}): Boolean =
        link?.send(JSONObject().put("type", type).apply(fill)) == true

    /** Отправить, как только появится связь (до 5 с). */
    fun sendSoon(type: String, fill: JSONObject.() -> Unit = {}) {
        scope.launch {
            withTimeoutOrNull(5000) {
                while (link?.connected != true) delay(100)
            }
            send(type, fill)
        }
    }

    private fun onLinkState(up: Boolean) {
        ui.update { it.copy(link = up, speakerOnline = up && it.speakerOnline) }
        if (up) {
            PhoneState.sendState(app)
            Media.sendCurrent()
            Bt.sendAll(app)
        }
    }

    // ── настройки колонки ──

    fun updateSettings(send: Boolean = true, f: (SpeakerSettings) -> SpeakerSettings) {
        ui.update { it.copy(settings = f(it.settings)) }
        if (send) this.send("settings") { put("values", ui.value.settings.toJson()) }
    }

    fun setMuted(pkg: String, muted: Boolean) {
        Prefs.mutedApps = if (muted) Prefs.mutedApps + pkg else Prefs.mutedApps - pkg
        ui.update { it.copy(muted = Prefs.mutedApps) }
    }

    // ── уведомления ──

    fun track(sbn: StatusBarNotification) {
        active[sbn.key] = sbn
    }

    fun onPosted(sbn: StatusBarNotification) {
        if (!::app.isInitialized || sbn.packageName == app.packageName) return
        val n = sbn.notification
        if (n.flags and Notification.FLAG_GROUP_SUMMARY != 0) return
        val cat = n.category.orEmpty()
        active[sbn.key] = sbn
        if (cat == Notification.CATEGORY_ALARM) {
            // будильник звонит, когда у уведомления часов есть полноэкранный режим
            if (n.fullScreenIntent != null && ringingAlarmKey != sbn.key) {
                ringingAlarmKey = sbn.key
                send("alarm") { put("event", "ringing") }
            }
            return
        }
        val extras = n.extras
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        val text = (extras.getCharSequence(Notification.EXTRA_BIG_TEXT)
            ?: extras.getCharSequence(Notification.EXTRA_TEXT))?.toString().orEmpty()
        if (cat == Notification.CATEGORY_CALL) callerName = title
        if (sbn.isOngoing && cat != Notification.CATEGORY_CALL) return
        if (title.isEmpty() && text.isEmpty()) return
        rememberApp(sbn.packageName)
        if (sbn.packageName in Prefs.mutedApps) return
        val hash = (title + "\u0000" + text).hashCode()
        if (lastSent[sbn.key] == hash) return  // мессенджеры перевыкладывают то же самое
        lastSent[sbn.key] = hash
        val canReply = n.actions?.any { !it.remoteInputs.isNullOrEmpty() } == true
        send("notif") {
            put("key", sbn.key)
            put("app", sbn.packageName)
            put("app_name", appName(sbn.packageName))
            put("title", title.take(80))
            put("text", text.take(300))
            put("category", cat)
            put("can_reply", canReply)
            put("when", n.`when`)
            put("actions", JSONArray(n.actions?.map { it.title?.toString().orEmpty() } ?: emptyList<String>()))
        }
    }

    fun onRemoved(sbn: StatusBarNotification) {
        active.remove(sbn.key)
        lastSent.remove(sbn.key)
        if (sbn.key == ringingAlarmKey) {
            ringingAlarmKey = null
            send("alarm") { put("event", "dismissed") }
        } else {
            send("notif_gone") { put("key", sbn.key) }
        }
    }

    /** Ответить в чат из уведомления — то же, что кнопка «Ответить». */
    fun reply(key: String, text: String): Boolean {
        val sbn = active[key] ?: return false
        val action = sbn.notification.actions?.firstOrNull { !it.remoteInputs.isNullOrEmpty() } ?: return false
        val intent = Intent()
        val results = Bundle()
        action.remoteInputs.forEach { results.putCharSequence(it.resultKey, text) }
        RemoteInput.addResultsToIntent(action.remoteInputs, intent, results)
        return try {
            action.actionIntent.send(NotifListener.instance ?: app, 0, intent)
            true
        } catch (e: PendingIntent.CanceledException) {
            false
        }
    }

    /** Нажать кнопку уведомления, в названии которой есть одно из слов. */
    fun pressAction(words: List<String>, key: String? = null, category: String? = null): Boolean {
        val sbn = when {
            !key.isNullOrEmpty() -> active[key]
            category != null -> active.values.lastOrNull { it.notification.category == category }
            else -> null
        } ?: return false
        val action = sbn.notification.actions?.firstOrNull { a ->
            val t = a.title?.toString()?.lowercase().orEmpty()
            words.any { w -> w.isNotBlank() && t.contains(w.lowercase()) }
        } ?: return false
        return try {
            action.actionIntent.send()
            true
        } catch (e: PendingIntent.CanceledException) {
            false
        }
    }

    private fun rememberApp(pkg: String) {
        val seen = Prefs.seenApps
        if (pkg in seen) return
        Prefs.seenApps = seen + pkg
        ui.update { it.copy(seenApps = Prefs.seenApps) }
    }

    fun appName(pkg: String): String = appNames.getOrPut(pkg) {
        try {
            val pm = app.packageManager
            pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
        } catch (e: Exception) {
            pkg
        }
    }

    // ── команды от сервера ──

    private fun onServerMessage(m: JSONObject) {
        when (m.optString("type")) {
            "settings" -> {
                val values = m.optJSONObject("values")
                val speaker = m.optJSONObject("speaker")
                ui.update {
                    it.copy(
                        settings = values?.let { v -> SpeakerSettings.from(v, it.settings) } ?: it.settings,
                        speakerOnline = speaker?.optBoolean("online") ?: it.speakerOnline,
                        speakerFps = speaker?.optInt("fps") ?: it.speakerFps,
                        pcOnline = m.optBoolean("pc", it.pcOnline),
                    )
                }
            }
            "ring" -> Finder.ring(app, m.optBoolean("on", true))
            "reply" -> reply(m.optString("key"), m.optString("text"))
            "action" -> pressAction(listOf(m.optString("title")), key = m.optString("key"))
            "dismiss" -> NotifListener.instance?.cancelNotification(m.optString("key"))
            "media" -> Media.command(m.optString("cmd"))
            "call" -> Calls.decline(app, m.optString("sms").takeIf { it.isNotBlank() })
            "open_url" -> showLink(m.optString("url"))
        }
    }

    /** Ссылка с компьютера: уведомление, по нажатию — браузер. */
    private fun showLink(url: String) {
        if (url.isBlank()) return
        val open = PendingIntent.getActivity(
            app, url.hashCode(), Intent(Intent.ACTION_VIEW, Uri.parse(url)),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val n = Notification.Builder(app, App.CH_ALERT)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("Ссылка с компьютера")
            .setContentText(url)
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        app.getSystemService(NotificationManager::class.java).notify(url.hashCode(), n)
    }
}
