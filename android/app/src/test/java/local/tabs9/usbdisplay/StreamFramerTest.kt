package local.tabs9.usbdisplay

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.EOFException
import java.io.InputStream
import java.net.SocketTimeoutException

class StreamFramerTest {
    private fun packet(type: Int, body: ByteArray): ByteArray {
        val size = 1 + body.size
        return byteArrayOf((size ushr 24).toByte(), (size ushr 16).toByte(), (size ushr 8).toByte(), size.toByte(),
            type.toByte()) + body
    }
    private fun frame(seq: Int, au: ByteArray) =
        packet(1, byteArrayOf((seq ushr 24).toByte(), (seq ushr 16).toByte(), (seq ushr 8).toByte(), seq.toByte()) + au)
    private fun heartbeat(n: Int) = packet(2, byteArrayOf(0, 0, 0, n.toByte()))

    /** Delivers one byte per read(): the worst fragmentation a socket can produce. */
    private class Trickle(bytes: ByteArray) : InputStream() {
        private val inner = ByteArrayInputStream(bytes)
        override fun read(): Int = inner.read()
        override fun read(b: ByteArray, off: Int, len: Int): Int = inner.read(b, off, minOf(len, 1))
    }

    /** Returns [bytes], then throws a read deadline forever after. */
    private class ThenTimeout(bytes: ByteArray) : InputStream() {
        private val inner = ByteArrayInputStream(bytes)
        override fun read(): Int = throw UnsupportedOperationException()
        override fun read(b: ByteArray, off: Int, len: Int): Int {
            val n = inner.read(b, off, len)
            if (n < 0) throw SocketTimeoutException("read timed out")
            return n
        }
    }

    @Test
    fun fragmentedPacketsAreReassembledWhole() {
        val au = ByteArray(3000) { it.toByte() }
        val framer = StreamFramer(Trickle(frame(7, au) + heartbeat(1) + frame(8, au)), 1 shl 20)
        val first = framer.next()
        assertEquals(StreamFramer.TYPE_FRAME, first.type)
        assertEquals(7, first.seq())
        assertEquals(5 + au.size, first.size)
        assertTrue(first.buffer.copyOfRange(5, first.size).contentEquals(au))
        val beat = framer.next()
        assertEquals(StreamFramer.TYPE_HEARTBEAT, beat.type)
        assertEquals(5, beat.size)
        assertEquals(8, framer.next().seq())
        assertEquals(3, framer.packets)
    }

    @Test
    fun deadlineBetweenPacketsIsIdleNotAFault() {
        val framer = StreamFramer(ThenTimeout(heartbeat(1)), 1 shl 20)
        assertEquals(StreamFramer.TYPE_HEARTBEAT, framer.next().type)
        assertThrows(SocketTimeoutException::class.java) { framer.next() }
    }

    @Test
    fun deadlineInsideAPacketIsAFramingFailure() {
        val whole = frame(3, ByteArray(100))
        val framer = StreamFramer(ThenTimeout(whole.copyOfRange(0, 40)), 1 shl 20)
        val error = assertThrows(FramingException::class.java) { framer.next() }
        assertTrue(error.message!!.contains("inside a packet body"))
        val header = StreamFramer(ThenTimeout(whole.copyOfRange(0, 2)), 1 shl 20)
        assertTrue(assertThrows(FramingException::class.java) { header.next() }.message!!.contains("packet length"))
    }

    @Test
    fun endOfStreamIsEof() {
        val whole = frame(3, ByteArray(100))
        assertThrows(EOFException::class.java) { StreamFramer(ByteArrayInputStream(ByteArray(0)), 1 shl 20).next() }
        assertThrows(EOFException::class.java) {
            StreamFramer(ByteArrayInputStream(whole.copyOfRange(0, 60)), 1 shl 20).next()
        }
    }

    @Test
    fun impossibleLengthsAndTypesAreRejected() {
        assertThrows(FramingException::class.java) {
            StreamFramer(ByteArrayInputStream(byteArrayOf(0, 0, 0, 1, 1)), 1 shl 20).next()
        }
        assertThrows(FramingException::class.java) {
            StreamFramer(ByteArrayInputStream(byteArrayOf(0x7f, 0, 0, 0, 1)), 1 shl 20).next()
        }
        assertThrows(FramingException::class.java) {
            StreamFramer(ByteArrayInputStream(packet(9, byteArrayOf(1, 2, 3, 4))), 1 shl 20).next()
        }
        // A frame packet needs a sequence number and at least one byte of access unit.
        assertThrows(FramingException::class.java) {
            StreamFramer(ByteArrayInputStream(packet(1, byteArrayOf(0, 0, 0, 1))), 1 shl 20).next()
        }
        // A heartbeat is exactly type + 4-byte counter.
        assertThrows(FramingException::class.java) {
            StreamFramer(ByteArrayInputStream(packet(2, byteArrayOf(0, 0, 0))), 1 shl 20).next()
        }
    }
}
