package com.example.bestcam

import android.util.Log
import java.io.BufferedOutputStream
import java.io.IOException
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

class MjpegServer(private val port: Int = 8080) {

    private var serverSocket: ServerSocket? = null
    private val clients = CopyOnWriteArrayList<Socket>()
    // Store a BufferedOutputStream per client — created once at connect time
    private val clientStreams = ConcurrentHashMap<Socket, BufferedOutputStream>()
    private val executor = Executors.newCachedThreadPool()
    private var isRunning = false
    private var frameCount = 0L

    private val latestFrame = AtomicReference<ByteArray?>()
    private val lock = ReentrantLock()
    private val frameCondition = lock.newCondition()

    // Control channel hooks (wired up by MainActivity)
    var capabilityProvider: (() -> List<Capability>)? = null
    var resolutionListener: ((w: Int, h: Int, fps: Int) -> Unit)? = null
    private var controlSocket: ServerSocket? = null

    fun start() {
        if (isRunning) return
        isRunning = true
        
        // Main server accept loop
        executor.execute {
            try {
                serverSocket = ServerSocket(port)
                Log.d("MjpegServer", "Raw TCP Server started on port $port")
                
                while (isRunning) {
                    val socket = serverSocket?.accept() ?: break
                    handleNewClient(socket)
                }
            } catch (e: IOException) {
                Log.e("MjpegServer", "Server Error", e)
            }
        }

        // Dedicated sender loop
        executor.execute {
            sendLoop()
        }

        // Control channel on 8081: capability handshake + set_resolution.
        executor.execute {
            try {
                controlSocket = ServerSocket(8081)
                Log.d("MjpegServer", "Control server started on port 8081")
                while (isRunning) {
                    val socket = controlSocket?.accept() ?: break
                    handleControlClient(socket)
                }
            } catch (e: IOException) {
                Log.e("MjpegServer", "Control server error", e)
            }
        }
    }

    private fun handleControlClient(socket: Socket) {
        executor.execute {
            try {
                socket.tcpNoDelay = true
                socket.soTimeout = 5000
                val input = socket.getInputStream().bufferedReader()
                val output = socket.getOutputStream()
                val line = input.readLine() ?: return@execute
                Log.d("MjpegServer", "control cmd: $line")
                when {
                    line.startsWith("GET /capabilities") -> {
                        val caps = capabilityProvider?.invoke().orEmpty()
                        val sb = StringBuilder("HTTP/1.1 200 OK\r\n")
                        sb.append("Content-Type: application/json\r\n\r\n")
                        sb.append(capsToJson(caps))
                        output.write(sb.toString().toByteArray())
                        output.flush()
                    }
                    line.startsWith("set_resolution") -> {
                        val parts = line.trim().split(Regex("\\s+"))
                        if (parts.size >= 4) {
                            val w = parts[1].toIntOrNull()
                            val h = parts[2].toIntOrNull()
                            val fps = parts[3].toIntOrNull()
                            if (w != null && h != null && fps != null) {
                                resolutionListener?.invoke(w, h, fps)
                                Log.d("MjpegServer", "set_resolution ${w}x$h @ $fps")
                            }
                        }
                        output.write("OK\r\n".toByteArray())
                        output.flush()
                    }
                    else -> {
                        output.write("UNKNOWN\r\n".toByteArray())
                        output.flush()
                    }
                }
                socket.close()
            } catch (e: Exception) {
                Log.d("MjpegServer", "control client error", e)
                try { socket.close() } catch (ignore: Exception) {}
            }
        }
    }

    private fun capsToJson(caps: List<Capability>): String {
        val sb = StringBuilder("{\"resolutions\":[")
        caps.forEachIndexed { i, c ->
            if (i > 0) sb.append(',')
            sb.append("{\"w\":").append(c.w)
                .append(",\"h\":").append(c.h)
                .append(",\"max_fps\":").append(c.maxFps)
                .append(",\"encode_ms\":").append(c.encodeMs)
                .append('}')
        }
        sb.append("]}")
        return sb.toString()
    }

    private fun handleNewClient(socket: Socket) {
        executor.execute {
            try {
                socket.tcpNoDelay = true
                socket.sendBufferSize = 512 * 1024

                val outputStream = BufferedOutputStream(socket.getOutputStream(), 256 * 1024)
                
                // Write MJPEG HTTP Header once per client
                val header = "HTTP/1.0 200 OK\r\n" +
                        "Server: BestCam\r\n" +
                        "Connection: close\r\n" +
                        "Max-Age: 0\r\n" +
                        "Expires: 0\r\n" +
                        "Cache-Control: no-cache, private\r\n" +
                        "Pragma: no-cache\r\n" +
                        "Content-Type: multipart/x-mixed-replace; boundary=--boundary\r\n\r\n"
                
                outputStream.write(header.toByteArray())
                outputStream.flush()
                
                clients.add(socket)
                clientStreams[socket] = outputStream
                Log.d("MjpegServer", "New client connected. Total: ${clients.size}")
            } catch (e: Exception) {
                socket.close()
            }
        }
    }

    fun sendFrame(jpegData: ByteArray) {
        latestFrame.set(jpegData)
        lock.withLock {
            frameCondition.signalAll()
        }
    }

    private fun sendLoop() {
        while (isRunning) {
            val jpegData = lock.withLock {
                while (latestFrame.get() == null && isRunning) {
                    try {
                        frameCondition.await(100, TimeUnit.MILLISECONDS)
                    } catch (e: InterruptedException) {
                        return@withLock null
                    }
                }
                latestFrame.getAndSet(null)
            }

            if (jpegData != null && clients.isNotEmpty()) {
                broadcastFrame(jpegData)
            }
        }
    }

    private fun broadcastFrame(jpegData: ByteArray) {
        frameCount++

        val boundary = "--boundary\r\nContent-Type: image/jpeg\r\nContent-Length: ${jpegData.size}\r\n\r\n"
        val boundaryBytes = boundary.toByteArray()
        val footerBytes = "\r\n".toByteArray()
        
        val packet = ByteArray(boundaryBytes.size + jpegData.size + footerBytes.size)
        System.arraycopy(boundaryBytes, 0, packet, 0, boundaryBytes.size)
        System.arraycopy(jpegData, 0, packet, boundaryBytes.size, jpegData.size)
        System.arraycopy(footerBytes, 0, packet, boundaryBytes.size + jpegData.size, footerBytes.size)

        val iterator = clients.iterator()
        while (iterator.hasNext()) {
            val client = iterator.next()
            try {
                val out = clientStreams[client] ?: continue
                out.write(packet)
                out.flush()
            } catch (e: IOException) {
                Log.d("MjpegServer", "Client disconnected")
                try { client.close() } catch (ignore: Exception) {}
                clientStreams.remove(client)
                clients.remove(client)
            }
        }
    }

    fun getClientCount(): Int = clients.size
    fun getFrameCount(): Long = frameCount

    fun stopServer() {
        isRunning = false
        lock.withLock {
            frameCondition.signalAll()
        }
        for (client in clients) {
            try { client.close() } catch (ignore: Exception) {}
        }
        clients.clear()
        clientStreams.clear()
        try { serverSocket?.close() } catch (ignore: Exception) {}
        serverSocket = null
        try { controlSocket?.close() } catch (ignore: Exception) {}
        controlSocket = null
    }
}
