package com.thatsimpletech.tstdesk

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.WindowManager
import com.google.zxing.BarcodeFormat
import com.journeyapps.barcodescanner.CameraPreview
import com.journeyapps.barcodescanner.DecoratedBarcodeView
import com.journeyapps.barcodescanner.DefaultDecoderFactory

/**
 * Reads the pairing QR code shown on the computer (TD-5014). zxing decodes
 * on the phone, so nothing is downloaded. The library's own CaptureActivity
 * needs androidx.core for the camera permission, which this app does not
 * ship, so this plain Activity hosts the library's view and asks itself.
 */
class ScanActivity : Activity() {
    private var scanner: DecoratedBarcodeView? = null
    private var asked = false
    private var finished = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        asked = savedInstanceState?.getBoolean(KEY_ASKED) ?: false
        val view = DecoratedBarcodeView(this)
        view.barcodeView.decoderFactory = DefaultDecoderFactory(listOf(BarcodeFormat.QR_CODE))
        view.setStatusText(getString(R.string.scan_prompt))
        view.barcodeView.addStateListener(object : CameraPreview.StateListener {
            override fun previewSized() = Unit
            override fun previewStarted() = Unit
            override fun previewStopped() = Unit
            override fun cameraClosed() = Unit
            override fun cameraError(error: Exception) {
                finishWith(reason = ScanReason.ERROR)
            }
        })
        // One read, with no beep: the page takes it from here.
        view.decodeSingle { result -> finishWith(text = result.text ?: "") }
        // The prompt sits at the bottom, clear of the navigation bar.
        applySystemBarInsets(view)
        setContentView(view)
        scanner = view
        if (!hasCameraPermission() && !asked) {
            asked = true
            requestPermissions(arrayOf(Manifest.permission.CAMERA), PERMISSION_REQUEST)
        }
    }

    override fun onResume() {
        super.onResume()
        // Opening the camera without the permission is a camera error, not a prompt.
        if (hasCameraPermission() && !finished) scanner?.resume()
    }

    override fun onPause() {
        scanner?.pause()
        super.onPause()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putBoolean(KEY_ASKED, asked)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        if (requestCode != PERMISSION_REQUEST) return
        // Granted: onResume runs next and opens the camera.
        if (grantResults.firstOrNull() != PackageManager.PERMISSION_GRANTED) {
            finishWith(reason = ScanReason.PERMISSION)
        }
    }

    private fun hasCameraPermission(): Boolean =
        checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED

    private fun finishWith(text: String? = null, reason: String? = null) {
        if (finished) return
        finished = true
        val data = Intent()
        if (text != null) data.putExtra(EXTRA_TEXT, text)
        if (reason != null) data.putExtra(EXTRA_REASON, reason)
        setResult(if (text != null) RESULT_OK else RESULT_CANCELED, data)
        finish()
    }

    companion object {
        const val EXTRA_TEXT = "com.thatsimpletech.tstdesk.SCAN_TEXT"
        const val EXTRA_REASON = "com.thatsimpletech.tstdesk.SCAN_REASON"
        private const val KEY_ASKED = "asked_camera"
        private const val PERMISSION_REQUEST = 41
    }
}
