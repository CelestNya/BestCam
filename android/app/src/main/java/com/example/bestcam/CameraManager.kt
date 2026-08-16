package com.example.bestcam

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.hardware.camera2.CaptureRequest
import android.util.Log
import android.util.Range
import android.util.Size
import androidx.camera.core.*
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

class CameraManager(
    private val context: Context,
    private val lifecycleOwner: LifecycleOwner,
    private val previewView: PreviewView,
    private val server: MjpegServer
) {
    private var cameraProvider: ProcessCameraProvider? = null
    private var camera: androidx.camera.core.Camera? = null
    private var lensFacing = CameraSelector.LENS_FACING_BACK
    private var imageAnalyzer: ImageAnalysis? = null
    private var preview: Preview? = null
    
    private val cameraExecutor: ExecutorService = Executors.newFixedThreadPool(2)
    
    private var targetResolution = Size(1920, 1080)
    var quality: Int = 55
    var isBeautyFilterEnabled = false

    private val jpegOutStream = ThreadLocal.withInitial { ByteArrayOutputStream(200_000) }
    private var yRow = ByteArray(0)
    private var uRow = ByteArray(0)
    private var vRow = ByteArray(0)

    fun startCamera() {
        val cameraProviderFuture = ProcessCameraProvider.getInstance(context)
        cameraProviderFuture.addListener({
            try {
                cameraProvider = cameraProviderFuture.get()
                bindCameraUseCases()
            } catch (e: Exception) {
                Log.e("CameraManager", "Error starting camera", e)
            }
        }, ContextCompat.getMainExecutor(context))
    }

    private fun bindCameraUseCases() {
        val cameraProvider = cameraProvider ?: return
        
        val cameraSelector = CameraSelector.Builder()
            .requireLensFacing(lensFacing)
            .build()

        // Preview follows the user-selected resolution (1080p/720p).
        // NOTE: never combine setTargetResolution with setTargetAspectRatio —
        // CameraX throws IllegalArgumentException and the use case fails to
        // bind (black preview, crash on resolution switch).
        preview = Preview.Builder()
            .setTargetResolution(targetResolution)
            .build()
            .also {
                it.setSurfaceProvider(previewView.surfaceProvider)
            }

        // Analysis (the JPEG stream) runs at a fixed 720p (16:9) regardless of
        // the preview resolution: a 1080p (or 1940x1940 sensor-native) JPEG
        // compress takes ~60ms on phone CPUs -> ~15fps stream. 720p is ~2.5x
        // faster and reaches ~30fps, and is more than enough for ExVR's
        // 800x450 processing. STRATEGY_KEEP_ONLY_LATEST drops stale frames.
        // ResolutionSelector pins the input to 1280x720: this sensor's YUV
        // stream exposes it (plus 1920x1080/1600x720), but plain
        // setTargetResolution alone was observed to pick the 1940x1940 square
        // stream, which is capped at ~13fps on this ISP.
        val analysisSelector = ResolutionSelector.Builder()
            .setResolutionStrategy(
                ResolutionStrategy(
                    Size(1280, 720),
                    ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER
                )
            )
            .build()
        // CameraX (1.3.x) does not expose setTargetFrameRate; without an AE
        // fps hint this sensor's analysis stream defaults to a ~12fps range.
        // Pin the CaptureRequest to the fixed [30,30] range the device
        // advertises (aeAvailableTargetFpsRanges).
        val analysisBuilder = ImageAnalysis.Builder()
            .setResolutionSelector(analysisSelector)
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
        Camera2Interop.Extender(analysisBuilder)
            .setCaptureRequestOption(
                CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE,
                Range(30, 30)
            )
        imageAnalyzer = analysisBuilder.build()
            .also {
                it.setAnalyzer(cameraExecutor) { imageProxy ->
                    try {
                        processImage(imageProxy)
                    } catch (e: Exception) {
                        Log.e("CameraManager", "Processing error", e)
                        imageProxy.close()
                    }
                }
            }

        try {
            cameraProvider.unbindAll()
            camera = cameraProvider.bindToLifecycle(
                lifecycleOwner,
                cameraSelector,
                preview,
                imageAnalyzer
            )
        } catch (e: Exception) {
            Log.e("CameraManager", "Use case binding failed", e)
        }
    }

    @SuppressLint("UnsafeOptInUsageError")
    private fun processImage(imageProxy: ImageProxy) {
        val jpegData = imageProxy.toJpeg()
        if (jpegData != null) {
            server.sendFrame(jpegData)
        }
        imageProxy.close()
    }

    /** Convert the frame to a 1280x720 (16:9, center-cropped) JPEG.
     *
     * Some sensors only expose square/4:3 sizes (e.g. 1940x1940); compressing
     * those at full size takes ~60ms on phone CPUs -> ~15fps stream. The
     * YUV_420_888 planes are sampled straight into a 720p NV21 buffer
     * (crop+scale+format conversion in one pass, no full-size intermediate),
     * so the JPEG is ~2.5x smaller and the stream reaches ~30fps with a
     * correct 16:9 aspect for ExVR's 800x450 processing.
     */
    private fun ImageProxy.toJpeg(): ByteArray? {
        return try {
            val width = this.width
            val height = this.height

            // center-crop to 16:9
            val cropW = if (width * 9 > height * 16) height * 16 / 9 else width
            val cropH = if (width * 9 > height * 16) height else width * 9 / 16
            val x0 = (width - cropW) / 2
            val y0 = (height - cropH) / 2

            val scaled = ByteArray(1280 * 720 * 3 / 2)
            yuv420ToNv21Scaled(this, scaled, x0, y0, cropW, cropH, 1280, 720)

            if (isBeautyFilterEnabled) {
                applyBeautyFilter(scaled, 1280, 720)
            }

            val yuvImage = YuvImage(scaled, ImageFormat.NV21, 1280, 720, null)
            val out = jpegOutStream.get()!!
            out.reset()
            yuvImage.compressToJpeg(Rect(0, 0, 1280, 720), quality, out)
            out.toByteArray()
        } catch (e: Exception) {
            Log.e("CameraManager", "JPEG conversion failed", e)
            null
        }
    }

    /** Sample YUV_420_888 planes directly into a cropped+scaled NV21 buffer.
     *
     * Nearest-neighbour sampling; handles rowStride/pixelStride padding.
     * Each source row is bulk-read into a reusable row buffer once, then the
     * output pixels are sampled from the array — a plain byte[] index is
     * 10-50x cheaper than per-pixel ByteBuffer.get() JNI calls, which is the
     * difference between ~57ms and ~10ms per 1940x1940 frame.
     */
    private fun yuv420ToNv21Scaled(
        image: ImageProxy, dst: ByteArray,
        x0: Int, y0: Int, cw: Int, ch: Int, dw: Int, dh: Int
    ) {
        val yBuf = image.planes[0].buffer
        val uBuf = image.planes[1].buffer
        val vBuf = image.planes[2].buffer
        val yStride = image.planes[0].rowStride
        val uStride = image.planes[1].rowStride
        val vStride = image.planes[2].rowStride
        val uPix = image.planes[1].pixelStride
        val vPix = image.planes[2].pixelStride

        // Y plane
        if (yRow.size < yStride) yRow = ByteArray(yStride)
        for (y in 0 until dh) {
            val sy = y0 + y * ch / dh
            yBuf.position(sy * yStride)
            yBuf.get(yRow, 0, cw)
            val drow = y * dw
            for (x in 0 until dw) {
                dst[drow + x] = yRow[x0 + x * cw / dw]
            }
        }
        // UV plane (NV21: interleaved VU, one pair per 2x2 luma)
        val dstYSize = dw * dh
        val uvScaleX = cw.toFloat() / dw
        val uvScaleY = ch.toFloat() / dh
        val uvCw = cw / 2
        val uvStrideLen = uvCw * uPix
        if (uRow.size < uStride) uRow = ByteArray(uStride)
        if (vRow.size < vStride) vRow = ByteArray(uStride)
        for (y in 0 until dh / 2) {
            val srcY = y0 + ((y * 2 + 1) * uvScaleY).toInt()
            val uvy = srcY / 2
            uBuf.position(uvy * uStride)
            uBuf.get(uRow, 0, uvStrideLen)
            vBuf.position(uvy * vStride)
            vBuf.get(vRow, 0, uvStrideLen)
            val drow = dstYSize + y * dw
            for (x in 0 until dw / 2) {
                val sx = (x0 + ((x * 2 + 1) * uvScaleX).toInt()) / 2
                dst[drow + x * 2] = vRow[sx * vPix]
                dst[drow + x * 2 + 1] = uRow[sx * uPix]
            }
        }
    }


    private fun applyBeautyFilter(nv21: ByteArray, width: Int, height: Int): ByteArray {
        val brightnessBoost = 25
        
        // Dynamic skip: Only skip if resolution is high (1080p+)
        // This fixes the 720p performance dip by processing more detail at lower res
        val skip = if (width >= 1920) 2 else 1
        
        for (y in 0 until height step skip) {
            val offset = y * width
            for (x in 0 until width step skip) {
                val idx = offset + x
                val yVal = nv21[idx].toInt() and 0xFF
                
                // 1. Brightness
                val boostedY = Math.min(255, yVal + brightnessBoost).toByte()
                
                // 2. Horizontal Smoothing
                nv21[idx] = boostedY
                if (x + 1 < width && skip == 2) {
                    nv21[idx + 1] = boostedY
                }
            }
        }
        
        return nv21
    }

    fun switchCamera() {
        lensFacing = if (lensFacing == CameraSelector.LENS_FACING_BACK) {
            CameraSelector.LENS_FACING_FRONT
        } else {
            CameraSelector.LENS_FACING_BACK
        }
        bindCameraUseCases()
    }

    fun toggleFlash(on: Boolean) {
        camera?.cameraControl?.enableTorch(on)
    }

    fun setZoom(ratio: Float) {
        camera?.cameraControl?.setZoomRatio(ratio)
    }

    fun setResolution(width: Int, height: Int) {
        targetResolution = Size(width, height)
        if (cameraProvider != null) {
            bindCameraUseCases()
        } else {
            startCamera()
        }
    }

    fun shutdown() {
        cameraExecutor.shutdown()
        cameraProvider?.unbindAll()
    }
}
