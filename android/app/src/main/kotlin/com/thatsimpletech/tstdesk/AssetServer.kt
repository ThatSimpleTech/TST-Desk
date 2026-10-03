package com.thatsimpletech.tstdesk

import java.io.BufferedOutputStream
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.charset.StandardCharsets
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * Serves the bundled UI to the WebView on loopback.
 *
 * `http://127.0.0.1` is still a secure context, so MainActivity allows
 * mixed content. Without that, the page would refuse the daemon's
 * cleartext `ws://`. The socket stays on 127.0.0.1 so the shell is not
 * a network service. CSP and the attach parser confine the page.
 */
class AssetServer(
    private val bindHost: String = LOOPBACK_HOST,
    private val files: (String) -> ByteArray?,
) {
    private var socket: ServerSocket? = null
    private val running = AtomicBoolean(false)

    @Volatile
    var boundHost: String = ""
        private set

    @Volatile
    var port: Int = -1
        private set

    fun start() {
        check(bindHost == LOOPBACK_HOST) { "asset server binds loopback only" }
        val address = InetAddress.getByName(LOOPBACK_HOST)
        val server = ServerSocket(0, 50, address)
        socket = server
        boundHost = server.inetAddress.hostAddress ?: ""
        port = server.localPort
        running.set(true)
        thread(name = "tst-asset-server", isDaemon = true) {
            while (running.get()) {
                val client = try {
                    server.accept()
                } catch (_: Exception) {
                    break
                }
                thread(name = "tst-asset-conn", isDaemon = true) {
                    try {
                        serve(client)
                    } finally {
                        client.close()
                    }
                }
            }
        }
    }

    fun stop() {
        running.set(false)
        try {
            socket?.close()
        } catch (_: Exception) {
            // Closing the listening socket is what unblocks accept.
        }
    }

    private fun serve(client: Socket) {
        client.soTimeout = 5_000
        val input = BufferedReader(InputStreamReader(client.getInputStream(), StandardCharsets.ISO_8859_1))
        val request = input.readLine() ?: return
        if (request.length > 8_192) {
            write(client, 400, "Bad Request", ByteArray(0), "text/plain; charset=utf-8")
            return
        }
        var headerLines = 0
        while (headerLines < 64) {
            val line = input.readLine() ?: break
            if (line.isEmpty()) break
            headerLines++
        }
        val parts = request.split(" ")
        if (parts.size < 2) {
            write(client, 400, "Bad Request", ByteArray(0), "text/plain; charset=utf-8")
            return
        }
        val method = parts[0]
        if (method != "GET" && method != "HEAD") {
            write(client, 405, "Method Not Allowed", ByteArray(0), "text/plain; charset=utf-8")
            return
        }
        val asset = bundledAssetPath(parts[1].substringBefore('?'))
        if (asset == null) {
            write(client, 400, "Bad Request", ByteArray(0), "text/plain; charset=utf-8")
            return
        }
        val body = files(asset)
        if (body == null) {
            write(client, 404, "Not Found", ByteArray(0), "text/plain; charset=utf-8")
            return
        }
        val payload = if (method == "HEAD") ByteArray(0) else body
        write(client, 200, "OK", payload, assetMime(asset), contentLength = body.size)
    }

    private fun write(
        client: Socket,
        code: Int,
        reason: String,
        body: ByteArray,
        contentType: String,
        contentLength: Int = body.size,
    ) {
        val header = "HTTP/1.1 $code $reason\r\n" +
            "Content-Type: $contentType\r\n" +
            "Content-Length: $contentLength\r\n" +
            "Cache-Control: no-store\r\n" +
            "Connection: close\r\n\r\n"
        val out = BufferedOutputStream(client.getOutputStream())
        out.write(header.toByteArray(StandardCharsets.US_ASCII))
        out.write(body)
        out.flush()
    }

    companion object {
        const val LOOPBACK_HOST = "127.0.0.1"
    }
}
