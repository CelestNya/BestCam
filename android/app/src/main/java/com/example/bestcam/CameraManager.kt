package com.example.bestcam

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.hardware.camera2.CameraCharacteristics
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
import android.os.Handler
import android.os.Looper
import com.example.bestcam.encoder.EncodedFrame
import com.example.bestcam.encoder.FrameEncoder
import com.example.bestcam.encoder.H264Encoder
import com.example.bestcam.encoder.JpegEncoder
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/** A negotiable stream option: resolution x frame-rate x codec that the phone
 * can actually sustain. Frame-rate is gated by real encode cost, not just HAL
 * capability. Aspect is implicit in w/h. */
data class Capability(val w: Int, val h: Int, val maxFps: Int, val encodeMs: Int, val codec: String = "mjpeg")

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

    // CameraX bind/unbind and PreviewView surface access must run on the main
    // thread; resolution switches arrive from the MjpegServer control thread.
    private val mainHandler = Handler(Looper.getMainLooper())
    
    private var targetResolution = Size(1920, 1080)
    var quality: Int = 55
    var isBeautyFilterEnabled = false

fun setHardwareEncoding(enabled: Boolean) {
        val newCodec = if (enabled) "h264" else "mjpeg"
        if (newCodec.equals(outCodec, ignoreCase = true)) return
        setStreamConfig(outW, outH, outFps, newCodec)
    }

    fun getStreamConfigString(): String {
        return "${outW}x${outH}@${outFps} ${outCodec.uppercase()}"
    }

    // Negotiated output (protocol) resolution/fps/codec.
    private var outW = 1280
    private var outH = 720
    private var outFps = 60
    private var outCodec = "mjpeg"
    private var encoder: FrameEncoder = JpegEncoder(quality, ::applyBeautyFilter)

    // Capability table (static probe, refreshed after encode-cost calibration)
    private val capsLock = Any()
    private var caps: List<Capability> = emptyList()
    private var calibNsPerPx = 0.0   // measured encode ns per pixel; 0 = uncalibrated

    // Default per-pixel encode cost estimate used before calibration
    // (Snapdragon 8-series soft-encodes ~7-12ns/px: 720p ≈ 7-11ms, 1080p ≈
    // 15-25ms, 480p ≈ 3ms). H.264 hardware encode is ~1.5ns/px.
    private val defaultNsPerPx = 12.0
    private val defaultH264NsPerPx = 1.5

    private var nv21Buffer: ByteArray? = null
    private val jpegOutStream = ThreadLocal.withInitial { ByteArrayOutputStream(200_000) }
    private var yRow = ByteArray(0)
    private var uRow = ByteArray(0)
    private var vRow = ByteArray(0)

    // profiling: per-frame encode cost (sampling + JPEG) over a window
    private var encSumNs = 0L
    private var encCount = 0

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
            .setTargetResolution(Size(outW, outH))
            .build()
            .also {
                it.setSurfaceProvider(previewView.surfaceProvider)
            }

        // The analysis stream is bound to a sensor size at least as large as the
        // negotiated output; frames are then center-cropped (to the output aspect)
        // and scaled to outW x outH in yuv420ToNv21Scaled. The crop+scale pass is
        // cheap (1:1 bulk copy when the crop matches), so binding a larger sensor
        // size than needed is fine — but the AE fps hint is what actually gates
        // capture rate, so it must follow the negotiated fps.
        val analysisSelector = ResolutionSelector.Builder()
            .setResolutionStrategy(
                ResolutionStrategy(
                    Size(outW, outH),
                    ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER
                )
            )
            .build()
        // CameraX (1.3.x) does not expose setTargetFrameRate; pin the AE fps
        // range instead. Range(30, 60) is accepted by this sensor's HAL and
        // unlocks 60fps; lower fps tiers pin a fixed range.
        val aeRange = when {
            outFps >= 55 -> Range(30, 60)
            outFps >= 27 -> Range(30, 30)
            else -> Range(15, 15)
        }
        val analysisBuilder = ImageAnalysis.Builder()
            .setResolutionSelector(analysisSelector)
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
        Camera2Interop.Extender(analysisBuilder)
            .setCaptureRequestOption(
                CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE,
                aeRange
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
        val t0 = System.nanoTime()
        val width = imageProxy.width
        val height = imageProxy.height

        // center-crop to the negotiated output aspect (usually 16:9)
        val cropW = if (width * 9 > height * 16) height * 16 / 9 else width
        val cropH = if (width * 9 > height * 16) height else width * 9 / 16
        val x0 = (width - cropW) / 2
        val y0 = (height - cropH) / 2

        val scaled = ByteArray(outW * outH * 3 / 2)
        yuv420ToNv21Scaled(imageProxy, scaled, x0, y0, cropW, cropH, outW, outH)
        val tSample = System.nanoTime()

        if (isBeautyFilterEnabled) {
            applyBeautyFilter(scaled, outW, outH)
        }

        val encoded = encoder.encode(scaled, outW, outH)
        val tEnc = System.nanoTime()
        if (encoded != null) {
            server.sendFrame(encoded)
        } else {
            Log.w("CameraManager", "encoder returned null for ${outW}x${outH}")
        }
        imageProxy.close()

        encSumNs += tEnc - t0
        if (++encCount >= 30) {
            Log.i("CameraManager",
                "avg encode ${encSumNs / encCount / 1_000_000}ms/frame over $encCount frames")
            calibrate(encSumNs, encCount)
            encSumNs = 0
            encCount = 0
        }
    }

    /** Create or recreate the encoder to match the negotiated codec and size. */
    private fun createEncoder() {
        try {
            encoder.release()
        } catch (_: Exception) {
        }
        encoder = when (outCodec.lowercase()) {
            "h264" -> {
                try {
                    H264Encoder(outW, outH, outFps)
                } catch (e: Exception) {
                    Log.e("CameraManager", "H264 encoder failed, falling back to JPEG", e)
                    outCodec = "mjpeg"
                    JpegEncoder(quality, ::applyBeautyFilter)
                }
            }
            else -> JpegEncoder(quality, ::applyBeautyFilter)
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
        val dstYSize = dw * dh

        if (cw == dw && ch == dh) {
            // 1:1 fast path: the crop matches the 720p target (e.g. 1280x960 ->
            // center-crop 1280x720), so bulk-copy rows instead of per-pixel
            // sampling. Y rows copy straight through; UV rows still interleave
            // V/U into NV21.
            if (yRow.size < yStride) yRow = ByteArray(yStride)
            for (y in 0 until dh) {
                yBuf.position((y0 + y) * yStride)
                yBuf.get(yRow, 0, cw)
                System.arraycopy(yRow, 0, dst, y * dw, dw)
            }
            val uvCw = cw / 2
            val uvStrideLen = uvCw * uPix
            if (uRow.size < uStride) uRow = ByteArray(uStride)
            if (vRow.size < vStride) vRow = ByteArray(uStride)
            for (y in 0 until dh / 2) {
                val uvy = (y0 + 2 * y + 1) / 2
                uBuf.position(uvy * uStride)
                uBuf.get(uRow, 0, uvStrideLen)
                vBuf.position(uvy * vStride)
                vBuf.get(vRow, 0, uvStrideLen)
                val drow = dstYSize + y * dw
                for (x in 0 until dw / 2) {
                    dst[drow + x * 2] = vRow[x * vPix]
                    dst[drow + x * 2 + 1] = uRow[x * uPix]
                }
            }
            return
        }

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

    private fun yuv420ToNv21(image: ImageProxy, nv21: ByteArray) {
        val width = image.width
        val height = image.height
        val planes = image.planes
        val yBuffer = planes[0].buffer
        val uBuffer = planes[1].buffer
        val vBuffer = planes[2].buffer

        // Copy Y plane
        val yRowStride = planes[0].rowStride
        if (yRowStride == width) {
            yBuffer.get(nv21, 0, width * height)
        } else {
            for (row in 0 until height) {
                yBuffer.position(row * yRowStride)
                yBuffer.get(nv21, row * width, width)
            }
        }

        // Copy interleaved U/V planes (NV21 format is YYYY... VUVU...)
        val vRowStride = planes[2].rowStride
        val vPixelStride = planes[2].pixelStride
        val uRowStride = planes[1].rowStride
        val uPixelStride = planes[1].pixelStride
        
        var pos = width * height
        
        if (vPixelStride == 2 && vBuffer.remaining() == (width * height / 2 - 1)) {
            vBuffer.get(nv21, pos, vBuffer.remaining())
        } else {
            for (row in 0 until height / 2) {
                for (col in 0 until width / 2) {
                    val vIdx = row * vRowStride + col * vPixelStride
                    val uIdx = row * uRowStride + col * uPixelStride
                    nv21[pos++] = vBuffer.get(vIdx)
                    nv21[pos++] = uBuffer.get(uIdx)
                }
            }
        }    }

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
        setStreamConfig(width, height, outFps, outCodec)
    }

    /** Rebind the camera pipeline to a negotiated output (resolution x fps x codec).
     * Returns the closest supported option if the requested one is not in the
     * capability table (never fails). */
    fun setStreamConfig(width: Int, height: Int, fps: Int, codec: String = "mjpeg") {
        val cap = pickCapability(width, height, fps, codec)
        val w = cap?.w ?: width
        val h = cap?.h ?: height
        val f = cap?.maxFps ?: fps
        val c = (cap?.codec ?: codec).lowercase()
        if (w == outW && h == outH && f == outFps && c == outCodec) return
        outW = w
        outH = h
        outFps = f
        outCodec = c
        createEncoder()
        Log.i("CameraManager", "setStreamConfig -> ${w}x$h @ $f fps [$outCodec]")
        if (cameraProvider != null) {
            mainHandler.post { bindCameraUseCases() }
        } else {
            mainHandler.post { startCamera() }
        }
    }

    /** Enumerate what this sensor can really sustain. HAL stream sizes give
     * the resolution candidates; per-pixel encode cost (calibrated after the
     * first window, estimated before) gates the achievable frame rate.
     * Frame-rate options are conditional: a combination only appears if the
     * phone can actually sustain it. */
    fun probeCapabilities(): List<Capability> {
        synchronized(capsLock) {
            if (caps.isNotEmpty()) return caps
        }
        val mgr = context.getSystemService(Context.CAMERA_SERVICE) as android.hardware.camera2.CameraManager
        val chars = try {
            val id = camera2Id(mgr)
            mgr.getCameraCharacteristics(id)
        } catch (e: Exception) {
            Log.e("CameraManager", "probe failed", e)
            return defaultCaps()
        }
        val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        if (map == null) {
            Log.e("CameraManager", "no stream config map")
            return defaultCaps()
        }
        val sizes = map.getOutputSizes(ImageFormat.YUV_420_888)
        Log.i("CameraManager", "raw YUV sizes: ${sizes.joinToString { "${it.width}x${it.height}" }}")
        // Keep every stream size in a sane range (no aspect dedup: 1080p and
        // 720p are both 16:9 and must both be options). The HAL's
        // minFrameDuration is a conservative static table (30fps for all YUV
        // sizes here, contradicting the measured 60fps), so frame rates are
        // gated by measured/estimated encode cost instead — except 1080p-tier
        // sizes, which we cap at 30fps until proven faster.
        val filtered = sizes
            .filter { it.width >= 480 && it.width <= 1920 && it.height >= 270 && it.height <= 1080 }
            .sortedByDescending { it.width.toLong() * it.height }
        Log.i("CameraManager", "filtered: ${filtered.joinToString { "${it.width}x${it.height}" }}")
        val out = mutableListOf<Capability>()
        filtered.forEach { size ->
            val pixels = size.width.toLong() * size.height
            val encodeMs = estimateEncodeMs(pixels)
            val fpsFromEncode = 1000.0 / encodeMs
            val is1080pTier = pixels > 1280L * 720
            val maxFps = if (is1080pTier) 30.0 else minOf(60.0, fpsFromEncode)
            Log.d("CameraManager", "probe ${size.width}x${size.height}: enc=${"%.1f".format(encodeMs)}ms fpsEnc=${"%.1f".format(fpsFromEncode)} max=$maxFps calib=$calibNsPerPx")
            if (maxFps >= 24) {
                val tier = if (maxFps >= 55) 60 else if (maxFps >= 27) 30 else 15
                out.add(Capability(size.width, size.height, tier, (encodeMs + 0.5).toInt(), "mjpeg"))
            }
            // H.264 hardware encode is fast enough for 60fps at all sizes here;
            // 1080p-tier is still capped at 30fps to mirror the companion/driver
            // default and keep bandwidth reasonable.
            val h264Ms = (defaultH264NsPerPx * pixels / 1e6 + 0.5).toInt().coerceAtLeast(1)
            val h264Fps = if (is1080pTier) 30 else 60
            out.add(Capability(size.width, size.height, h264Fps, h264Ms, "h264"))
        }
        synchronized(capsLock) {
            if (caps.isEmpty()) caps = out
        }
        Log.i("CameraManager", "capabilities: $out")
        return out
    }

    /** Resolve a requested combo against the capability table: exact match, or
     * same aspect at the closest size, or any closest size. */
    fun pickCapability(w: Int, h: Int, fps: Int, codec: String = "mjpeg"): Capability? {
        val table = probeCapabilities()
        if (table.isEmpty()) return null
        val codecNorm = codec.lowercase()
        return table.firstOrNull { it.w == w && it.h == h && it.maxFps == fps && it.codec == codecNorm }
            ?: table.firstOrNull { it.w == w && it.h == h && it.codec == codecNorm }
            ?: table.firstOrNull { it.w == w && it.h == h }
            ?: table.firstOrNull { aspectKey(it.w, it.h) == aspectKey(w, h) && it.codec == codecNorm }
            ?: table.minByOrNull { kotlin.math.abs(it.w.toLong() * it.h - w.toLong() * h) }
    }

    /** Feed real encode timings into the calibration; recompute the table for
     * the active codec only. */
    fun calibrate(encodeNs: Long, frames: Int) {
        if (frames <= 0 || outW * outH <= 0) return
        calibNsPerPx = encodeNs.toDouble() / (frames * outW.toLong() * outH)
        val re = estimateEncodeMs(outW.toLong() * outH)
        Log.i("CameraManager", "calibrated ${"%.1f".format(calibNsPerPx)} ns/px (encode ~${re}ms @ ${outW}x$outH)")
        synchronized(capsLock) {
            if (caps.isEmpty()) return
            caps = caps.map { c ->
                if (c.codec != outCodec) return@map c
                val ms = estimateEncodeMs(c.w.toLong() * c.h)
                val fpsFromEncode = 1000.0 / ms
                val is1080pTier = c.w.toLong() * c.h > 1280L * 720
                val max = if (is1080pTier) 30.0 else minOf(60.0, fpsFromEncode)
                val tier = if (max >= 55) 60 else if (max >= 27) 30 else 15
                Capability(c.w, c.h, tier, (ms + 0.5).toInt(), c.codec)
            }
        }
    }

    private fun estimateEncodeMs(pixels: Long): Double =
        (if (calibNsPerPx > 0) calibNsPerPx else defaultNsPerPx) * pixels / 1e6

    private fun aspectKey(w: Int, h: Int): Long {
        val g = gcd(w, h)
        return (w / g).toLong() shl 20 or (h / g).toLong()
    }

    private fun gcd(a: Int, b: Int): Int = if (b == 0) a else gcd(b, a % b)

    private fun camera2Id(mgr: android.hardware.camera2.CameraManager): String {
        val facing = if (lensFacing == CameraSelector.LENS_FACING_BACK)
            CameraCharacteristics.LENS_FACING_BACK else CameraCharacteristics.LENS_FACING_FRONT
        return mgr.cameraIdList.firstOrNull { id ->
            try {
                mgr.getCameraCharacteristics(id)
                    .get(CameraCharacteristics.LENS_FACING) == facing
            } catch (e: Exception) { false }
        } ?: mgr.cameraIdList.first()
    }

    private fun defaultCaps(): List<Capability> = listOf(
        Capability(1920, 1080, 30, 25, "mjpeg"),
        Capability(1280, 720, 60, 10, "mjpeg"),
        Capability(640, 480, 60, 4, "mjpeg"),
        Capability(1920, 1080, 30, 4, "h264"),
        Capability(1280, 720, 60, 2, "h264"),
        Capability(640, 480, 60, 1, "h264")
    )

    fun shutdown() {
        cameraExecutor.shutdown()
        cameraProvider?.unbindAll()
    }
}
