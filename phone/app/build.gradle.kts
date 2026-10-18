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
        versionCode = 2
        versionName = "0.2.0"
        val port = env.getProperty("JARVIS_PORT", "8765")
        buildConfigField("String", "SERVER_URL", "\"ws://$serverHost:$port/phone\"")
        buildConfigField("String", "TOKEN", "\"${env.getProperty("JARVIS_DEVICE_TOKEN", "")}\"")
    }

    buildTypes {
        // R8 и в отладочной сборке: без него APK ~31 МБ (Compose целиком) и не пролезает
        // на телефон через Remote Control (лимит 30 МБ). Наш код не трогаем — proguard-rules.pro.
        debug {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
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
    implementation(platform("androidx.compose:compose-bom:2025.10.00"))
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.activity:activity-compose:1.10.1")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    // 1.5.0-alpha16 — последняя с открытыми Expressive API, которой хватает compileSdk 35 и AGP 8.7
    // (в стабильной 1.4.0 они internal, с 1.5.0-alpha20 нужны SDK 37 и AGP 9.1)
    implementation("androidx.compose.material3:material3:1.5.0-alpha16")
    implementation("androidx.compose.material:material-icons-core")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
}
