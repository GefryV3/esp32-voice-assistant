package dev.jarvis.bridge

import android.content.ComponentName
import android.content.Context
import android.media.MediaMetadata
import android.media.session.MediaController
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

/**
 * Служба уведомлений: её держит сама система, пока у приложения есть доступ к
 * уведомлениям. Через неё же видно, что играет (плеер на колонке).
 */
class NotifListener : NotificationListenerService() {
    override fun onListenerConnected() {
        instance = this
        Bridge.start(this)
        activeNotifications?.forEach { Bridge.track(it) }
        Media.attach(this)
    }

    override fun onListenerDisconnected() {
        instance = null
        Media.detach()
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) = Bridge.onPosted(sbn)

    override fun onNotificationRemoved(sbn: StatusBarNotification) = Bridge.onRemoved(sbn)

    companion object {
        @Volatile
        var instance: NotifListener? = null
    }
}

/** Что играет на телефоне и управление плеером. */
object Media {
    private var msm: MediaSessionManager? = null
    private var ctrl: MediaController? = null
    private var component: ComponentName? = null

    private val sessions = MediaSessionManager.OnActiveSessionsChangedListener { pick(it) }

    private val callback = object : MediaController.Callback() {
        override fun onPlaybackStateChanged(state: PlaybackState?) = sendCurrent()
        override fun onMetadataChanged(metadata: MediaMetadata?) = sendCurrent()
        override fun onSessionDestroyed() {
            pick(component?.let { msm?.getActiveSessions(it) })
        }
    }

    fun attach(ctx: Context) {
        val comp = ComponentName(ctx, NotifListener::class.java)
        component = comp
        msm = ctx.getSystemService(MediaSessionManager::class.java)
        try {
            msm?.addOnActiveSessionsChangedListener(sessions, comp)
            pick(msm?.getActiveSessions(comp))
        } catch (e: SecurityException) {
        }
    }

    fun detach() {
        try {
            msm?.removeOnActiveSessionsChangedListener(sessions)
        } catch (e: Exception) {
        }
        ctrl?.unregisterCallback(callback)
        ctrl = null
    }

    private fun pick(list: List<MediaController>?) {
        val next = list?.firstOrNull { it.playbackState?.state == PlaybackState.STATE_PLAYING } ?: list?.firstOrNull()
        if (next?.sessionToken != ctrl?.sessionToken) {
            ctrl?.unregisterCallback(callback)
            ctrl = next
            next?.registerCallback(callback, Handler(Looper.getMainLooper()))
        }
        sendCurrent()
    }

    fun sendCurrent() {
        val c = ctrl
        val st = c?.playbackState
        val md = c?.metadata
        val playing = st?.state == PlaybackState.STATE_PLAYING
        // позиция «сейчас»: плеер сообщает её на момент последнего обновления
        val pos = if (st == null) 0L
        else st.position + if (playing) ((SystemClock.elapsedRealtime() - st.lastPositionUpdateTime) * st.playbackSpeed).toLong() else 0L
        Bridge.send("media") {
            put("state", when {
                st == null -> "none"
                playing -> "playing"
                st.state == PlaybackState.STATE_PAUSED -> "paused"
                else -> "stopped"
            })
            put("title", md?.getString(MediaMetadata.METADATA_KEY_TITLE).orEmpty())
            put("artist", md?.getString(MediaMetadata.METADATA_KEY_ARTIST).orEmpty())
            put("app", c?.packageName?.let { Bridge.appName(it) }.orEmpty())
            put("pos", pos)
            put("dur", md?.getLong(MediaMetadata.METADATA_KEY_DURATION) ?: 0L)
            put("at", System.currentTimeMillis())
        }
    }

    fun command(cmd: String) {
        val c = ctrl ?: return
        val t = c.transportControls
        when (cmd) {
            "play" -> t.play()
            "pause" -> t.pause()
            "toggle" -> if (c.playbackState?.state == PlaybackState.STATE_PLAYING) t.pause() else t.play()
            "next" -> t.skipToNext()
            "prev" -> t.skipToPrevious()
        }
    }
}
