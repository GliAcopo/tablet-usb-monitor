package local.tabs9.usbdisplay

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.SystemClock
import android.util.Log
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * How much sound to hold back before the speaker: pure Kotlin, so the rules
 * are unit-testable without a device.
 *
 * The player writes to a small AudioTrack with blocking writes, so the
 * tablet's own sound clock paces it; the cushion is the queue of chunks
 * waiting in front of the track, which the player can measure exactly
 * (the track's play head cannot be trusted for that: on the MatePad Paper
 * the music output consumes in large periods and the head jumps).
 *
 * The computer's clock and the tablet's drift apart, and the network
 * delivers in bursts: a queue longer than the cushion by [EXCESS_MS] drops
 * a chunk to get the latency back. Waiting for a chunk in mid-stream means
 * the cushion was too small for this link, so it grows and refills; a long
 * wait is the host pausing (it sends nothing for digital silence) and only
 * refills it.
 */
class AudioJitter {
    companion object {
        const val START_MS = 50
        const val STEP_MS = 20
        const val MAX_MS = 250
        const val EXCESS_MS = 40
        /** Waiting longer than this for a chunk, while playing, ran the track dry. */
        const val STALL_MS = 60L
        /** Waiting longer than this is the host pausing, not the link stalling. */
        const val PAUSE_GAP_MS = 250L
        const val DROP = -1
    }

    var targetMs = START_MS; private set
    var underruns = 0; private set
    var drops = 0; private set

    /**
     * For a chunk just taken from the queue, with [queuedMs] still behind it
     * and [waitedMs] spent waiting for it: [DROP], or how many milliseconds to
     * let the queue fill before writing it (0: write at once).
     */
    fun onChunk(queuedMs: Int, waitedMs: Long, playing: Boolean): Int {
        if (!playing) return targetMs
        if (waitedMs > STALL_MS) {
            if (waitedMs < PAUSE_GAP_MS) {
                underruns++
                targetMs = minOf(targetMs + STEP_MS, MAX_MS)
            }
            return targetMs
        }
        if (queuedMs > targetMs + EXCESS_MS) {
            drops++
            return DROP
        }
        return 0
    }
}

/**
 * Plays the computer's sound: 48 kHz stereo 16-bit PCM chunks from the video
 * socket (StreamFramer.TYPE_AUDIO), on a thread of its own so a slow speaker
 * never holds up the picture. The host side is src/audio.py.
 */
class AudioPlayer {
    companion object {
        const val TAG = "tabs9Audio"
        const val RATE = 48000
        const val BYTES_PER_FRAME = 4
        const val BYTES_PER_MS = RATE * BYTES_PER_FRAME / 1000
        /** No sound for this long: pause the track so the tablet's audio path can sleep. */
        const val IDLE_PAUSE_MS = 1500L
    }

    private val queue = ArrayBlockingQueue<ByteArray>(256)
    private val queuedBytes = AtomicInteger()
    private val jitter = AudioJitter()
    @Volatile private var running = true
    var chunks = 0L; private set

    private val thread = Thread({ play() }, "tabs9-audio").apply { start() }

    /** Network thread: hand over one chunk (copied; dropped if the player is far behind). */
    fun feed(buffer: ByteArray, offset: Int, length: Int) {
        val usable = length - length % BYTES_PER_FRAME
        if (usable <= 0 || !running) return
        if (queue.offer(buffer.copyOfRange(offset, offset + usable))) queuedBytes.addAndGet(usable)
    }

    fun release() {
        running = false
        thread.interrupt()
    }

    private fun buildTrack(): AudioTrack {
        val minimum = AudioTrack.getMinBufferSize(RATE, AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_16BIT)
        return AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                .build())
            .setAudioFormat(AudioFormat.Builder()
                .setSampleRate(RATE)
                .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .build())
            // Small: whatever sits in the track is latency nobody can trim.
            .setBufferSizeInBytes(maxOf(minimum * 2, 40 * BYTES_PER_MS))
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
    }

    private fun play() {
        val track = try { buildTrack() } catch (e: Exception) {
            Log.w(TAG, "No audio output: ${e.message}")
            return
        }
        Log.i(TAG, "Audio track: ${track.bufferSizeInFrames * 1000 / RATE} ms buffer")
        var playing = false
        var lastChunk = SystemClock.elapsedRealtime()
        try {
            while (running) {
                val asked = SystemClock.elapsedRealtime()
                val chunk = try { queue.poll(250, TimeUnit.MILLISECONDS) } catch (_: InterruptedException) { break }
                val now = SystemClock.elapsedRealtime()
                if (chunk == null) {
                    if (playing && now - lastChunk > IDLE_PAUSE_MS) {
                        track.pause(); track.flush()
                        playing = false
                        Log.i(TAG, "Sound paused (cushion ${jitter.targetMs} ms, " +
                            "${jitter.underruns} underruns, ${jitter.drops} drops so far)")
                    }
                    continue
                }
                lastChunk = now
                val queuedMs = queuedBytes.addAndGet(-chunk.size) / BYTES_PER_MS
                val action = jitter.onChunk(queuedMs, now - asked, playing)
                if (action == AudioJitter.DROP) continue
                if (action > 0) {
                    try { Thread.sleep(action.toLong()) } catch (_: InterruptedException) { break }
                }
                if (!playing) {
                    track.play()
                    playing = true
                    Log.i(TAG, "Playing the computer's sound (cushion ${jitter.targetMs} ms)")
                }
                track.write(chunk, 0, chunk.size)   // blocks: the tablet's clock sets the pace
                chunks++
            }
        } finally {
            try { track.stop() } catch (_: Exception) {}
            track.release()
        }
    }
}
