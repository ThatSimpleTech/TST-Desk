package com.thatsimpletech.tstdesk

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URL

class ShellPolicyTest {
    @Test
    fun rootAndRoutesAreTheShellDocument() {
        assertEquals("index.html", bundledAssetPath("/"))
        assertEquals("index.html", bundledAssetPath(""))
        assertEquals("index.html", bundledAssetPath("/quick-entry"))
        assertTrue(isShellDocumentAlias("/index.html"))
        assertTrue(isShellDocumentAlias("/index.html?x=1"))
        assertFalse(isShellDocumentAlias("/"))
        assertFalse(isShellDocumentAlias("/_app/index.html"))
    }

    @Test
    fun filePathsStayInsideTheBundle() {
        assertEquals(
            "_app/immutable/entry/start.js",
            bundledAssetPath("/_app/immutable/entry/start.js"),
        )
    }

    @Test
    fun traversalIsRefused() {
        assertNull(bundledAssetPath("/../secret"))
        assertNull(bundledAssetPath("/%2e%2e/secret"))
        assertNull(bundledAssetPath("/_app/../../etc/passwd"))
        assertNull(bundledAssetPath("/..\\secret"))
        assertNull(bundledAssetPath("/%"))
    }

    @Test
    fun pageStaysOnTheLoopbackOrigin() {
        assertTrue(isShellPage("http://127.0.0.1:43123/index.html", 43123))
        assertFalse(isShellPage("http://127.0.0.1:43123/index.html", 1))
        assertFalse(isShellPage("http://10.0.0.5:43123/", 43123))
        assertFalse(isShellPage("http://0.0.0.0:43123/", 43123))
        assertFalse(isShellPage("https://127.0.0.1:43123/", 43123))
        assertFalse(isShellPage("http://user@127.0.0.1:43123/", 43123))
        assertFalse(isShellPage("file:///android_asset/index.html", 43123))
        assertFalse(isShellPage("javascript:alert(1)", 43123))
    }

    @Test(expected = IllegalStateException::class)
    fun serverRefusesANonLoopbackBind() {
        AssetServer(files = { null }, bindHost = "0.0.0.0").start()
    }

    @Test
    fun serverBindsLoopbackAndServesOnlyTheBundle() {
        val files = mapOf(
            "index.html" to "<html>ok</html>".toByteArray(),
            "_app/a.js" to "console.log(1)".toByteArray(),
        )
        val server = AssetServer { files[it] }
        server.start()
        try {
            assertEquals(AssetServer.LOOPBACK_HOST, server.boundHost)
            assertFalse(server.boundHost == "0.0.0.0")
            assertTrue(server.port in 1..65535)
            val page = http("http://${server.boundHost}:${server.port}/")
            assertEquals(200, page.code)
            assertEquals("<html>ok</html>", page.text)
            val alias = http("http://${server.boundHost}:${server.port}/index.html")
            assertEquals(302, alias.code)
            assertEquals("/", alias.location)
            val script = http("http://${server.boundHost}:${server.port}/_app/a.js")
            assertEquals(200, script.code)
            assertTrue(script.type.startsWith("text/javascript"))
            assertEquals(400, rawStatus(server.boundHost, server.port, "/../secret"))
            assertEquals(404, http("http://${server.boundHost}:${server.port}/missing.js").code)
            assertEquals(200, http("http://${server.boundHost}:${server.port}/sessions").code)
        } finally {
            server.stop()
        }
    }

    @Test
    fun theRememberedPortIsBoundAgain() {
        val memory = FakePortMemory()
        val first = AssetServer(ports = memory) { null }
        first.start()
        val port = first.port
        first.stop()
        assertEquals("a fresh port is remembered", port, memory.port)
        val second = AssetServer(ports = memory) { null }
        second.start()
        try {
            assertEquals("same origin as last launch", port, second.port)
            assertEquals(1, memory.writes)
        } finally {
            second.stop()
        }
    }

    @Test
    fun aBusyRememberedPortFallsBackAndRemembersTheNewOne() {
        ServerSocket(0, 1, InetAddress.getByName(AssetServer.LOOPBACK_HOST)).use { squatter ->
            val memory = FakePortMemory(squatter.localPort)
            val server = AssetServer(ports = memory) { null }
            server.start()
            try {
                assertNotEquals(squatter.localPort, server.port)
                assertTrue(server.port in 1..65535)
                assertEquals(server.port, memory.port)
            } finally {
                server.stop()
            }
        }
    }

    @Test
    fun aJunkRememberedPortIsIgnored() {
        for (junk in listOf(-1, 0, 80, 70_000)) {
            val memory = FakePortMemory(junk)
            val server = AssetServer(ports = memory) { null }
            server.start()
            try {
                assertTrue(server.port >= AssetServer.MIN_REMEMBERED_PORT)
                assertEquals(server.port, memory.port)
            } finally {
                server.stop()
            }
        }
    }
}

private class FakePortMemory(var port: Int = 0) : PortMemory {
    var writes = 0
    override fun recall() = port
    override fun remember(port: Int) {
        this.port = port
        writes++
    }
}

private data class HttpBody(val code: Int, val type: String, val text: String, val location: String?)

private fun http(raw: String): HttpBody {
    val connection = URL(raw).openConnection() as HttpURLConnection
    connection.instanceFollowRedirects = false
    connection.connectTimeout = 2_000
    connection.readTimeout = 2_000
    val code = connection.responseCode
    val type = connection.contentType ?: ""
    val location = connection.getHeaderField("Location")
    val stream = if (code >= 400) connection.errorStream else connection.inputStream
    val text = stream?.bufferedReader()?.use { it.readText() } ?: ""
    connection.disconnect()
    return HttpBody(code, type, text, location)
}

private fun rawStatus(host: String, port: Int, path: String): Int {
    Socket(host, port).use { socket ->
        socket.soTimeout = 2_000
        val out = socket.getOutputStream()
        val request = "GET $path HTTP/1.1\r\nHost: $host:$port\r\nConnection: close\r\n\r\n"
        out.write(request.toByteArray())
        out.flush()
        val status = BufferedReader(InputStreamReader(socket.getInputStream())).readLine() ?: return -1
        return status.split(" ").getOrNull(1)?.toIntOrNull() ?: -1
    }
}
