package local.tabs9.usbdisplay

import java.io.EOFException
import java.io.IOException
import java.io.InputStream
import java.net.SocketTimeoutException

/**
 * Splits the video socket into packets: 4-byte big-endian length, then a
 * payload whose first byte is the type.
 *
 * Pure Kotlin so the framing rules are unit-testable without a device:
 *  - a read deadline that expires *between* packets is an idle socket and is
 *    surfaced as [SocketTimeoutException] for the caller to judge (the host
 *    may simply have nothing to send);
 *  - a deadline that expires *inside* a packet, an impossible length, or an
 *    unknown type is a [FramingException]: the byte stream can no longer be
 *    trusted and the connection must be rebuilt.
 */
class FramingException(message: String) : IOException(message)

class StreamFramer(private val input: InputStream, private val maxPayload: Int) {
    companion object {
        const val TYPE_CONFIG = 0
        const val TYPE_FRAME = 1
        /** type byte + 4-byte counter; carries no video, keeps the socket alive while idle. */
        const val TYPE_HEARTBEAT = 2
        /** type byte + 4-byte big-endian sequence number, then the access unit. */
        const val FRAME_HEADER_SIZE = 5
        const val HEARTBEAT_SIZE = 5
    }

    /** Payload of the last packet; [Packet.size] bytes of it are valid. Reused across packets. */
    class Packet(val type: Int, val buffer: ByteArray, val size: Int) {
        /** Host sequence number of a frame packet. */
        fun seq(): Int = ((buffer[1].toInt() and 0xFF) shl 24) or ((buffer[2].toInt() and 0xFF) shl 16) or
            ((buffer[3].toInt() and 0xFF) shl 8) or (buffer[4].toInt() and 0xFF)
    }

    private val header = ByteArray(4)
    private var payload = ByteArray(512 * 1024)
    var packets = 0L; private set
    var bytes = 0L; private set

    /** Blocks for the next packet. See the class comment for what each exception means. */
    fun next(): Packet {
        readExact(header, 4, "packet length")
        val size = ((header[0].toInt() and 0xFF) shl 24) or ((header[1].toInt() and 0xFF) shl 16) or
            ((header[2].toInt() and 0xFF) shl 8) or (header[3].toInt() and 0xFF)
        if (size <= 1 || size > maxPayload + 1) throw FramingException("packet length $size")
        if (payload.size < size) payload = ByteArray(size + size / 2)
        readExact(payload, size, "packet body")
        packets++
        bytes += 4 + size
        val type = payload[0].toInt() and 0xFF
        when (type) {
            TYPE_CONFIG -> {}
            TYPE_FRAME -> if (size <= FRAME_HEADER_SIZE) throw FramingException("frame packet of $size bytes")
            TYPE_HEARTBEAT -> if (size != HEARTBEAT_SIZE) throw FramingException("heartbeat of $size bytes")
            else -> throw FramingException("packet type $type")
        }
        return Packet(type, payload, size)
    }

    private fun readExact(buffer: ByteArray, length: Int, what: String) {
        var offset = 0
        while (offset < length) {
            val read = try {
                input.read(buffer, offset, length - offset)
            } catch (e: SocketTimeoutException) {
                // Only a deadline before the first byte of a packet is "idle".
                if (offset == 0 && what == "packet length") throw e
                throw FramingException("deadline expired inside a $what after $offset bytes")
            }
            if (read < 0) throw EOFException("stream closed inside a $what after $offset bytes")
            offset += read
        }
    }
}
