plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// The phone shell bundles the same static UI, built so its CSP and attach
// parser are the remote-shell policy. Desktop `ui/build` is a different mode.
val uiDir = rootProject.projectDir.resolve("../ui")
val generatedUi = layout.buildDirectory.dir("generated/ui-assets")

tasks.register<Exec>("buildAndroidUi") {
    workingDir = uiDir
    commandLine("npm", "run", "build", "--", "--mode", "android")
    inputs.dir(uiDir.resolve("src"))
    inputs.file(uiDir.resolve("package.json"))
    inputs.file(uiDir.resolve("package-lock.json"))
    inputs.file(uiDir.resolve("vite.config.ts"))
    outputs.dir(uiDir.resolve("build-android"))
}

tasks.register<Copy>("syncUiAssets") {
    dependsOn("buildAndroidUi")
    from(uiDir.resolve("build-android"))
    into(generatedUi)
    exclude("**/*.map")
}

android {
    namespace = "com.thatsimpletech.tstdesk"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.thatsimpletech.tstdesk"
        minSdk = 26
        targetSdk = 35
        versionCode = 12
        versionName = "0.2.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Sideload artifact. This debug keystore is not a Play upload key.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    lint {
        checkReleaseBuilds = false
    }

    // aapt's default ignore pattern drops directories named `_…`. SvelteKit
    // emits the client bundle as `_app`, so that pattern would ship an empty shell.
    androidResources {
        ignoreAssetsPattern = "!.svn:!.git:!.ds_store:!*.scc:.*:!CVS:!thumbs.db:!picasa.ini:!*~"
    }

    sourceSets.getByName("main").assets.srcDir(generatedUi)
}

tasks.withType<org.jetbrains.kotlin.gradle.tasks.KotlinCompile>().configureEach {
    compilerOptions.jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
}

tasks.configureEach {
    if (name == "syncUiAssets" || name == "buildAndroidUi") return@configureEach
    if (name.contains("Assets") || name.contains("Lint")) {
        dependsOn("syncUiAssets")
    }
}

dependencies {
    // TD-5014: offline QR scanner (zxing decoding on the phone, no Play
    // services module download). Pinned exactly; it brings zxing core only.
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    testImplementation("junit:junit:4.13.2")
    // Real org.json for JVM tests; android.jar's copy is a stub there.
    testImplementation("org.json:json:20240303")
}
