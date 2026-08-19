package com.example.bestcam.encoder

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.util.Log
import java.nio.ByteBuffer

/**
 * Hardware H.264 encoder using MediaCodec.
 *
 * Input is NV21; it is converted in-place to NV12 (U/V swapped) because the
 * Qualcomm H.264 encoder advertises COLOR_FormatYUV420SemiPlanar. The encoder
 * is run in synchronous mode so that [encode] returns one access unit per call,
 * which keeps the streaming loop simple.
 *
 * If configure/start fails the constructor throws; the caller should catch the
 * exception and fall back to [JpegEncoder].
 */
class H264Encoder(
    private val width: Int,
    private val height: Int,
    private val fps: Int,
    bitrate: Int = 0
) : FrameEncoder {

    override val codec: String = "h264"

    private val codecInstance: MediaCodec
    private val bitRate = if (bitrate > 0) bitrate else defaultBitrate(width, height, fps)
    private val bufferInfo = MediaCodec.BufferInfo()
    private val presentationStep = 1_000_000L / fps  // 1us per frame
    private var presentationTimeUs = 0L
    private var released = false
    private val nv12SwapBuf = ByteArray(width * height * 3 / 2)
    private var spsPps: ByteArray? = null

    init {
        val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height).apply {
            setInteger(MediaFormat.KEY_BIT_RATE, bitRate)
            setInteger(MediaFormat.KEY_FRAME_RATE, fps)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 0) // every frame is a key frame (low-latency webcam)
            setInteger(
                MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420SemiPlanar
            )
        }
        codecInstance = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
        codecInstance.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        codecInstance.start()
    }

    override fun encode(yuv: ByteArray, width: Int, height: Int): EncodedFrame? {
        if (released || width != this.width || height != this.height) {
            Log.w("H264Encoder", "encode skipped: released=$released size=${width}x$height expected=${this.width}x${this.height}")
            return null
        }

        // Convert NV21 -> NV12 in-place (swap U/V pairs in the chroma plane).
        nv21ToNv12(yuv, nv12SwapBuf)

        val inputIndex = codecInstance.dequeueInputBuffer(10_000)
        if (inputIndex < 0) {
            Log.w("H264Encoder", "no input buffer available, draining")
            return drainOne()
        }
        val inputBuffer = codecInstance.getInputBuffer(inputIndex) ?: return drainOne()
        inputBuffer.clear()
        inputBuffer.put(nv12SwapBuf)
        codecInstance.queueInputBuffer(
            inputIndex, 0, nv12SwapBuf.size, presentationTimeUs, 0
        )
        presentationTimeUs += presentationStep

        val out = drainOne()
        if (out == null) {
            Log.d("H264Encoder", "no output for queued input (pts=$presentationTimeUs)")
        } else {
            Log.d("H264Encoder", "output ${out.data.size} bytes key=${out.isKeyFrame}")
        }
        return out
    }

    override fun release() {
        if (released) return
        released = true
        try {
            codecInstance.stop()
        } catch (_: Exception) {
        }
        codecInstance.release()
    }

    /**
     * Drain a single access unit. H.264 baseline has no B-frames, so each input
     * frame produces exactly one output frame (which may consist of multiple
     * NALs such as SPS+PPS+IDR for a keyframe).
     */
    private fun drainOne(): EncodedFrame? {
        val outIndex = codecInstance.dequeueOutputBuffer(bufferInfo, 30_000)
        return when {
            outIndex >= 0 -> {
                val buf = codecInstance.getOutputBuffer(outIndex) ?: return null
                val data = ByteArray(bufferInfo.size)
                buf.position(bufferInfo.offset)
                buf.limit(bufferInfo.offset + bufferInfo.size)
                buf.get(data)
                codecInstance.releaseOutputBuffer(outIndex, false)
                val key = (bufferInfo.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME) != 0
                // Prepend SPS/PPS so mid-stream clients can decode any key frame.
                val sps = spsPps
                val out = if (key && sps != null) {
                    sps + data
                } else {
                    data
                }
                EncodedFrame(out, codec, key)
            }
            outIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                val fmt = codecInstance.outputFormat
                val sps = fmt.getByteBuffer("csd-0")
                val pps = fmt.getByteBuffer("csd-1")
                val out = java.io.ByteArrayOutputStream()
                if (sps != null) {
                    val b = ByteArray(sps.remaining())
                    sps.get(b)
                    out.write(b)
                }
                if (pps != null) {
                    val b = ByteArray(pps.remaining())
                    pps.get(b)
                    out.write(b)
                }
                spsPps = out.toByteArray()
                Log.d("H264Encoder", "captured sps/pps ${spsPps?.size} bytes")
                drainOne()
            }
            outIndex == MediaCodec.INFO_TRY_AGAIN_LATER -> {
                Log.d("H264Encoder", "output not ready")
                null
            }
            else -> {
                Log.w("H264Encoder", "unexpected dequeueOutputBuffer result: $outIndex")
                null
            }
        }
    }

    private fun nv21ToNv12(nv21: ByteArray, nv12: ByteArray) {
        // Y plane is identical.
        System.arraycopy(nv21, 0, nv12, 0, width * height)
        // UV plane: swap V/U pairs.
        val uvStart = width * height
        val uvLen = width * height / 2
        for (i in uvStart until uvStart + uvLen step 2) {
            nv12[i] = nv21[i + 1]     // U
            nv12[i + 1] = nv21[i]     // V
        }
    }

    companion object {
        fun defaultBitrate(width: Int, height: Int, fps: Int): Int {
            // ~0.12 bits per pixel, typical for low-latency webcam H.264.
            return (width * height * fps * 0.12).toInt()
        }
    }
}
