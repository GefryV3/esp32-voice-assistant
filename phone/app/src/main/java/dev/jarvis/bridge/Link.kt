package dev.jarvis.bridge

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** Связь с сервером по WebSocket: рукопожатие как у колонки, переподключение с паузой. */
class Link(
    private val scope: CoroutineScope,
    private val onMessage: (JSONObject) -> Unit,
    private val onState: (Boolean) -> Unit,
) {
    private val client = OkHttpClient.Builder()
        .pingInterval(15, TimeUnit.SECONDS)
        .connectTimeout(5, TimeUnit.SECONDS)
        .build()
    private var ws: WebSocket? = null
    private var retryMs = 1000L
    private var reconnect: Job? = null

    @Volatile
    var connected = false
        private set

    fun start() = connect()

    /** Заново — например, после смены адреса сервера. */
    fun restart() {
        ws?.cancel()
        retryMs = 1000L
        connect()
    }

    fun send(msg: JSONObject): Boolean = connected && ws?.send(msg.toString()) == true

    private fun connect() {
        reconnect?.cancel()
        val req = try {
            Request.Builder().url(Prefs.serverUrl).build()
        } catch (e: IllegalArgumentException) {
            schedule()
            return
        }
        ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                val hello = JSONObject()
                    .put("type", "hello")
                    .put("proto", 1)
                    .put("token", Prefs.token)
                    .put("device", "phone")
                    .put("fw", BuildConfig.VERSION_NAME)
                    .put("caps", JSONArray().put("phone"))
                webSocket.send(hello.toString())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val msg = try {
                    JSONObject(text)
                } catch (e: Exception) {
                    return
                }
                if (msg.optString("type") == "welcome") {
                    connected = true
                    retryMs = 1000L
                    onState(true)
                }
                onMessage(msg)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = lost(webSocket)

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = lost(webSocket)
        })
    }

    private fun lost(socket: WebSocket) {
        if (socket !== ws) return
        if (connected) {
            connected = false
            onState(false)
        }
        schedule()
    }

    private fun schedule() {
        reconnect?.cancel()
        reconnect = scope.launch {
            delay(retryMs)
            retryMs = (retryMs * 2).coerceAtMost(30_000L)
            connect()
        }
    }
}
