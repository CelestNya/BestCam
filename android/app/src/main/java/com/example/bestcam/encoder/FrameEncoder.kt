package com.example.bestcam.encoder

/**
 * Abstraction over the phone-side frame encoder.
 *
 * Two implementations are provided:
 *  - [JpegEncoder]: CPU software JPEG (baseline, universally compatible).
 *  - [H264Encoder]: MediaCodec hardware H.264 encoder (performance priority).
 *
 * The output is fed to the streaming server as a multipart chunk. The codec
 * tag is used by the server to set the correct MIME type and by the companion
 * to pick the right decoder.
 */
interface FrameEncoder {
    /** Codec token: "mjpeg" or "h264". */
    val codec: String

    /**
     * Encode a YUV frame into a compressed access unit.
     *
     * @param yuv NV21-encoded YUV buffer, dimensions [width] x [height].
     * @param width frame width in pixels.
     * @param height frame height in pixels.
     * @return encoded bytes, or null on failure (caller should fall back).
     */
    fun encode(yuv: ByteArray, width: Int, height: Int): EncodedFrame?

    /** Release encoder resources. The encoder must not be reused after this. */
    fun release()
}

/** One encoded frame plus metadata for the stream server. */
data class EncodedFrame(
    val data: ByteArray,
    val codec: String,
    val isKeyFrame: Boolean = false
) {
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is EncodedFrame) return false
        return data.contentEquals(other.data) &&
                codec == other.codec &&
                isKeyFrame == other.isKeyFrame
    }

    override fun hashCode(): Int {
        var result = data.contentHashCode()
        result = 31 * result + codec.hashCode()
        result = 31 * result + isKeyFrame.hashCode()
        return result
    }
}
