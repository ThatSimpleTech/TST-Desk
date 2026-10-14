package com.thatsimpletech.tstdesk

import android.content.Context
import java.io.BufferedOutputStream
import java.io.BufferedReader
import java.io.IOException
import java.io.InputStreamReader
import java.net.InetAddress
import java.net.InetSocketAddress
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
    private val ports: PortMemory = PortMemory.NONE,
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
        val remembered = ports.recall()
        val server = bindLoopback(address, remembered)
        socket = server
        boundHost = server.inetAddress.hostAddress ?: ""
        port = server.localPort
        if (port != remembered) ports.remember(port)
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
        if (isShellDocumentAlias(parts[1])) {
            redirect(client, "/")
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

    private fun redirect(client: Socket, location: String) {
        val header = "HTTP/1.1 302 Found\r\n" +
            "Location: $location\r\n" +
            "Content-Length: 0\r\n" +
            "Cache-Control: no-store\r\n" +
            "Connection: close\r\n\r\n"
        val out = BufferedOutputStream(client.getOutputStream())
        out.write(header.toByteArray(StandardCharsets.US_ASCII))
        out.flush()
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

        /** Below this an app cannot bind, so a remembered value there is junk. */
        const val MIN_REMEMBERED_PORT = 1024
    }
}

/**
 * Bind the remembered port first (TD-5014). The page origin includes the
 * port and the WebView keeps its storage per origin, so a new port every
 * launch was a new, empty origin. A busy port falls back to a random one.
 */
internal fun bindLoopback(address: InetAddress, preferred: Int): ServerSocket {
    if (preferred in AssetServer.MIN_REMEMBERED_PORT..65535) {
        try {
            return listen(address, preferred)
        } catch (_: IOException) {
            // Taken by something else this launch: a random port, remembered next.
        }
    }
    return listen(address, 0)
}

private fun listen(address: InetAddress, port: Int): ServerSocket {
    val server = ServerSocket()
    try {
        // Last launch's closed connections can sit in TIME_WAIT on this port.
        server.reuseAddress = true
        server.bind(InetSocketAddress(address, port), 50)
    } catch (e: IOException) {
        server.close()
        throw e
    }
    return server
}

/** Where the shell keeps its loopback port between launches. */
interface PortMemory {
    fun recall(): Int
    fun remember(port: Int)

    companion object {
        val NONE: PortMemory = object : PortMemory {
            override fun recall() = 0
            override fun remember(port: Int) = Unit
        }
    }
}

/** Private app storage, next to the sealed attach target. */
class PrefsPortMemory(context: Context) : PortMemory {
    private val prefs = context.getSharedPreferences("shell", Context.MODE_PRIVATE)

    override fun recall(): Int = try {
        prefs.getInt(KEY, 0)
    } catch (_: ClassCastException) {
        0
    }

    override fun remember(port: Int) {
        prefs.edit().putInt(KEY, port).apply()
    }

    private companion object {
        const val KEY = "asset_port"
    }
}
