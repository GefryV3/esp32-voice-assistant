import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Адрес сервера и токен — из server/.env, чтобы в приложении ничего не вводить руками.
// Адрес можно поменять и потом, в самом приложении.
val env = Properties().apply {
    val f = rootProject.file("../server/.env")
    if (f.exists()) {
        f.readLines().forEach { line ->
            val t = line.trim()
            if (t.isNotEmpty() && !t.startsWith("#") && "=" in t) {
                val (k, v) = t.split("=", limit = 2)
                setProperty(k.trim(), v.trim().trim('"', '\''))
            }
        }
    }
}
val serverHost = (project.findProperty("serverHost") as String?) ?: env.getProperty("JARVIS_SERVER", "192.168.178.86")

android {
    namespace = "dev.jarvis.bridge"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.jarvis.bridge"
        minSdk = 31
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
        val port = env.getProperty("JARVIS_PORT", "8765")
        buildConfigField("String", "SERVER_URL", "\"ws://$serverHost:$port/phone\"")
        buildConfigField("String", "TOKEN", "\"${env.getProperty("JARVIS_DEVICE_TOKEN", "")}\"")
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2024.12.01"))
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
}
