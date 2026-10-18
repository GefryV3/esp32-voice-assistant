package dev.jarvis.bridge

import android.app.Activity
import android.app.Application
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Bundle
import android.os.IBinder
import android.widget.Toast

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        Prefs.init(this)
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CH_LINK, "Связь с колонкой", NotificationManager.IMPORTANCE_MIN).apply {
                setShowBadge(false)
            }
        )
        nm.createNotificationChannel(NotificationChannel(CH_ALERT, "Колонка", NotificationManager.IMPORTANCE_HIGH))
        Bridge.start(this)
    }

    companion object {
        const val CH_LINK = "link"
        const val CH_ALERT = "alert"
    }
}

/** Служба «на переднем плане»: не даёт системе выгрузить приложение, пока оно нужно колонке. */
class BridgeService : Service() {
    override fun onCreate() {
        super.onCreate()
        val n = Notification.Builder(this, App.CH_LINK)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("Колонка на связи")
            .setOngoing(true)
            .build()
        startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        Bridge.start(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int) = START_STICKY

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        fun start(ctx: Context) {
            try {
                ctx.startForegroundService(Intent(ctx, BridgeService::class.java))
            } catch (e: Exception) {
                // фоновый запуск запрещён — не страшно: мост живёт и в службе уведомлений
            }
        }
    }
}

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) BridgeService.start(context)
    }
}

/** «Поделиться → Открыть на компьютере». */
class ShareActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val text = intent?.getStringExtra(Intent.EXTRA_TEXT)?.trim()
        if (text.isNullOrEmpty()) {
            Toast.makeText(this, "Нечего открывать", Toast.LENGTH_SHORT).show()
        } else {
            Bridge.start(this)
            Bridge.sendSoon("share") { put("text", text) }
            Toast.makeText(this, "Открываю на компьютере", Toast.LENGTH_SHORT).show()
        }
        finish()
    }
}
