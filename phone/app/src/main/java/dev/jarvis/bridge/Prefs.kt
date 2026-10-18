package dev.jarvis.bridge

import android.content.Context
import android.content.SharedPreferences

/** Настройки самого приложения (то, что касается колонки, хранит сервер). */
object Prefs {
    private lateinit var sp: SharedPreferences

    fun init(ctx: Context) {
        sp = ctx.getSharedPreferences("bridge", Context.MODE_PRIVATE)
    }

    var serverUrl: String
        get() = sp.getString("server", null) ?: BuildConfig.SERVER_URL
        set(v) = sp.edit().putString("server", v).apply()

    val token: String get() = BuildConfig.TOKEN

    /** Приложения, уведомления которых не дублируются на колонку. */
    var mutedApps: Set<String>
        get() = sp.getStringSet("muted", emptySet())!!.toSet()
        set(v) = sp.edit().putStringSet("muted", v).apply()

    /** Приложения, которые уже присылали уведомления, — для списка в настройках. */
    var seenApps: Set<String>
        get() = sp.getStringSet("seen", emptySet())!!.toSet()
        set(v) = sp.edit().putStringSet("seen", v).apply()
}
