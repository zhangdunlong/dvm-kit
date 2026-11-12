import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// 签名配置从 gradle.properties / 环境变量读取，仓库内不含密钥。
// 未提供时回退 debug 签名，保证任何人都能直接 assembleDebug / assembleRelease。
val signProps = Properties().apply {
    val f = rootProject.file("signing.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
fun secret(key: String): String =
    (signProps.getProperty(key) ?: System.getenv(key) ?: project.findProperty(key) ?: "").toString()

val hasReleaseKey = secret("DVM_STORE_FILE").isNotBlank() && rootProject.file(secret("DVM_STORE_FILE")).exists()

android {
    namespace = "com.dvm.kit"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.dvm.kit"
        minSdk = 24
        targetSdk = 34
        versionCode = 2
        versionName = "1.1.0"
        resValue("string", "app_name", "DVM 控制台")
    }

    signingConfigs {
        if (hasReleaseKey) {
            create("release") {
                storeFile = rootProject.file(secret("DVM_STORE_FILE"))
                storePassword = secret("DVM_STORE_PASSWORD")
                keyAlias = secret("DVM_KEY_ALIAS")
                keyPassword = secret("DVM_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        getByName("release") {
            isMinifyEnabled = false
            isShrinkResources = false
            // 有正式密钥就用正式密钥；没有则用 debug 密钥签名，
            // 保证 assembleRelease 产物可直接安装试用（仅限自用，勿用于商店分发）
            signingConfig = if (hasReleaseKey) signingConfigs.getByName("release")
                            else signingConfigs.getByName("debug")
        }
        getByName("debug") {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        buildConfig = true
    }
    packaging {
        resources.excludes += setOf("META-INF/*.kotlin_module")
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.webkit:webkit:1.11.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
}
