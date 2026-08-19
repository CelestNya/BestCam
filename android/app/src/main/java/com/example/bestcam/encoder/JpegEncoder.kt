package com.example.bestcam.encoder

import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import java.io.ByteArrayOutputStream

/**
 * CPU software JPEG encoder.
 *
 * This is the baseline path: any Android device can run it, but it consumes
 * significant CPU at high resolutions. Kept as the automatic fallback when
 * hardware encoding is unavailable or fails.
 */
class JpegEncoder(
    private val quality: Int = 55,
    private val beautyFilter: ((nv21: ByteArray, width: Int, height: Int) -> Unit)? = null
) : FrameEncoder {

    override val codec: String = "mjpeg"

    private val streamPool = ThreadLocal<ByteArrayOutputStream>()

    override fun encode(yuv: ByteArray, width: Int, height: Int): EncodedFrame? {
        return try {
            beautyFilter?.invoke(yuv, width, height)
            val out = streamPool.get() ?: ByteArrayOutputStream().also { streamPool.set(it) }
            out.reset()
            val yuvImage = YuvImage(yuv, ImageFormat.NV21, width, height, null)
            yuvImage.compressToJpeg(Rect(0, 0, width, height), quality, out)
            EncodedFrame(out.toByteArray(), codec)
        } catch (e: Exception) {
            null
        }
    }

    override fun release() {
        streamPool.get()?.reset()
    }
}
