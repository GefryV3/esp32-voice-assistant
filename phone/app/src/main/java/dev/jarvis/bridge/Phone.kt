package dev.jarvis.bridge

import android.Manifest
import android.annotation.SuppressLint
import android.app.AlarmManager
import android.app.KeyguardManager
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.VibratorManager
import android.telecom.TelecomManager
import android.telephony.PhoneStateListener
import android.telephony.SmsManager
import android.telephony.TelephonyManager
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject

private fun Context.granted(p: String) = ContextCompat.checkSelfPermission(this, p) == PackageManager.PERMISSION_GRANTED

/** Экран, разблокировка, зарядка, батарея, будильник, «не беспокоить». */
object PhoneState {
    @Volatile var screenOn = true
    @Volatile var charging = false
    @Volatile var battery = -1
    @Volatile var unlockedAt = 0L

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) {
            when (i.action) {
                Intent.ACTION_SCREEN_ON -> screenOn = true
                Intent.ACTION_SCREEN_OFF -> screenOn = false
                Intent.ACTION_USER_PRESENT -> unlockedAt = System.currentTimeMillis()
                Intent.ACTION_POWER_CONNECTED -> charging = true
                Intent.ACTION_POWER_DISCONNECTED -> charging = false
                Intent.ACTION_BATTERY_CHANGED -> {
                    val scale = i.getIntExtra(BatteryManager.EXTRA_SCALE, 100).coerceAtLeast(1)
                    val level = i.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) * 100 / scale
                    val plugged = i.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0
                    if (level == battery && plugged == charging) return  // приходит часто — шлём только перемены
                    battery = level
                    charging = plugged
                }
            }
            sendState(c)
        }
    }

    fun register(ctx: Context) {
        val f = IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_SCREEN_OFF)
            addAction(Intent.ACTION_USER_PRESENT)
            addAction(Intent.ACTION_POWER_CONNECTED)
            addAction(Intent.ACTION_POWER_DISCONNECTED)
            addAction(Intent.ACTION_BATTERY_CHANGED)
            addAction(AlarmManager.ACTION_NEXT_ALARM_CLOCK_CHANGED)
            addAction(NotificationManager.ACTION_INTERRUPTION_FILTER_CHANGED)
        }
        ContextCompat.registerReceiver(ctx, receiver, f, ContextCompat.RECEIVER_NOT_EXPORTED)
        screenOn = ctx.getSystemService(PowerManager::class.java).isInteractive
    }

    fun sendState(ctx: Context) {
        val am = ctx.getSystemService(AlarmManager::class.java)
        val nm = ctx.getSystemService(NotificationManager::class.java)
        val km = ctx.getSystemService(KeyguardManager::class.java)
        Bridge.send("state") {
            put("screen", screenOn)
            put("locked", km.isKeyguardLocked)
            put("charging", charging)
            put("battery", battery)
            put("unlocked_at", unlockedAt)
            put("next_alarm", am.nextAlarmClock?.triggerTime ?: 0L)
            put("dnd", nm.currentInterruptionFilter > NotificationManager.INTERRUPTION_FILTER_ALL)
        }
    }
}

/** Звонки: кто звонит, «сбрось», «перезвоню» по SMS. */
object Calls {
    @Volatile private var number: String? = null
    private var registered = false

    @Suppress("DEPRECATION")
    fun register(ctx: Context) {
        if (registered || !ctx.granted(Manifest.permission.READ_PHONE_STATE)) return
        registered = true
        val tm = ctx.getSystemService(TelephonyManager::class.java)
        Handler(Looper.getMainLooper()).post {
            tm.listen(object : PhoneStateListener() {
                override fun onCallStateChanged(state: Int, phoneNumber: String?) {
                    val s = when (state) {
                        TelephonyManager.CALL_STATE_RINGING -> "ringing"
                        TelephonyManager.CALL_STATE_OFFHOOK -> "active"
                        else -> "idle"
                    }
                    when (state) {
                        TelephonyManager.CALL_STATE_RINGING -> number = phoneNumber?.takeIf { it.isNotBlank() } ?: number
                        TelephonyManager.CALL_STATE_IDLE -> number = null
                    }
                    Bridge.send("call") {
                        put("state", s)
                        put("number", number.orEmpty())
                        put("name", Bridge.callerName)
                    }
                }
            }, PhoneStateListener.LISTEN_CALL_STATE)
        }
    }

    @SuppressLint("MissingPermission")
    fun decline(ctx: Context, sms: String?) {
        val to = number
        // сначала — кнопкой из уведомления звонилки, иначе — через систему
        val pressed = Bridge.pressAction(listOf("отклон", "сброс", "decline", "reject"), category = Notification.CATEGORY_CALL)
        if (!pressed && ctx.granted(Manifest.permission.ANSWER_PHONE_CALLS)) {
            try {
                @Suppress("DEPRECATION")
                ctx.getSystemService(TelecomManager::class.java).endCall()
            } catch (e: Exception) {
            }
        }
        if (!sms.isNullOrBlank() && to != null && ctx.granted(Manifest.permission.SEND_SMS)) {
            try {
                ctx.getSystemService(SmsManager::class.java).sendTextMessage(to, null, sms, null, null)
            } catch (e: Exception) {
            }
        }
    }
}

/** «Где мой телефон?» — громко, даже в беззвучном режиме. */
object Finder {
    private var player: MediaPlayer? = null
    private var stopJob: Job? = null
    private const val NOTIF_ID = 7

    fun ring(ctx: Context, on: Boolean) = if (on) start(ctx) else stop(ctx)

    private fun start(ctx: Context) {
        stop(ctx)
        val am = ctx.getSystemService(AudioManager::class.java)
        try {
            am.setStreamVolume(AudioManager.STREAM_ALARM, am.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0)
        } catch (e: Exception) {
        }
        val uri = RingtoneManager.getActualDefaultRingtoneUri(ctx, RingtoneManager.TYPE_RINGTONE)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
        player = try {
            MediaPlayer().apply {
                setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build()
                )
                setDataSource(ctx, uri)
                isLooping = true
                prepare()
                start()
            }
        } catch (e: Exception) {
            null
        }
        try {
            ctx.getSystemService(VibratorManager::class.java).defaultVibrator.vibrate(
                VibrationEffect.createWaveform(longArrayOf(0, 600, 400), 0)
            )
        } catch (e: Exception) {
        }
        val stop = PendingIntent.getBroadcast(
            ctx, 0, Intent(ctx, StopRingReceiver::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        val n = Notification.Builder(ctx, App.CH_ALERT)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("Я тут!")
            .setContentText("Колонка ищет телефон")
            .setOngoing(true)
            .setContentIntent(stop)
            .addAction(Notification.Action.Builder(null, "Нашёл", stop).build())
            .build()
        ctx.getSystemService(NotificationManager::class.java).notify(NOTIF_ID, n)
        stopJob = Bridge.scope.launch {
            delay(60_000)
            stop(ctx)
        }
    }

    fun stop(ctx: Context) {
        stopJob?.cancel()
        stopJob = null
        player?.run {
            try {
                stop()
            } catch (e: Exception) {
            }
            release()
        }
        if (player != null) Bridge.send("ring_stopped")
        player = null
        try {
            ctx.getSystemService(VibratorManager::class.java).defaultVibrator.cancel()
        } catch (e: Exception) {
        }
        ctx.getSystemService(NotificationManager::class.java).cancel(NOTIF_ID)
    }
}

class StopRingReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) = Finder.stop(context)
}

/** Заряд Bluetooth-наушников, которые подключены к телефону. */
object Bt {
    private const val ACTION_BATTERY = "android.bluetooth.device.action.BATTERY_LEVEL_CHANGED"
    private var registered = false

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) = sendAll(c)
    }

    fun register(ctx: Context) {
        if (registered) return
        registered = true
        val f = IntentFilter().apply {
            addAction(ACTION_BATTERY)
            addAction(BluetoothDevice.ACTION_ACL_CONNECTED)
            addAction(BluetoothDevice.ACTION_ACL_DISCONNECTED)
        }
        ContextCompat.registerReceiver(ctx, receiver, f, ContextCompat.RECEIVER_EXPORTED)
    }

    @SuppressLint("MissingPermission")
    fun sendAll(ctx: Context) {
        if (!ctx.granted(Manifest.permission.BLUETOOTH_CONNECT)) return
        val adapter = ctx.getSystemService(BluetoothManager::class.java)?.adapter ?: return
        val list = JSONArray()
        for (d in adapter.bondedDevices.orEmpty()) {
            // заряд и «подключено» в Android есть, но спрятаны — достаём так же, как сама шторка
            val level = try {
                d.javaClass.getMethod("getBatteryLevel").invoke(d) as Int
            } catch (e: Exception) {
                -1
            }
            val connected = try {
                d.javaClass.getMethod("isConnected").invoke(d) as Boolean
            } catch (e: Exception) {
                false
            }
            if (connected && level in 0..100) list.put(JSONObject().put("name", d.name).put("level", level))
        }
        Bridge.send("bt") { put("devices", list) }
    }
}

/** Что разрешено приложению — для экрана «Доступы». */
object Permissions {
    val runtime: Array<String> = buildList {
        if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
        add(Manifest.permission.READ_PHONE_STATE)
        add(Manifest.permission.READ_CALL_LOG)
        add(Manifest.permission.ANSWER_PHONE_CALLS)
        add(Manifest.permission.SEND_SMS)
        add(Manifest.permission.BLUETOOTH_CONNECT)
    }.toTypedArray()

    data class Status(val listener: Boolean, val runtime: Boolean, val battery: Boolean) {
        val all get() = listener && runtime && battery
    }

    fun check(ctx: Context) = Status(
        listener = NotificationManagerCompat.getEnabledListenerPackages(ctx).contains(ctx.packageName),
        runtime = runtime.all { ctx.granted(it) },
        battery = ctx.getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(ctx.packageName),
    )
}
