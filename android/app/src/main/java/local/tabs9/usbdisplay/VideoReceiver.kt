package local.tabs9.usbdisplay

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.view.Surface
import android.view.SurfaceView
import kotlinx.coroutines.*
import java.net.Socket
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference

class VideoReceiver {
    companion object {
        const val HOST = "127.0.0.1"
        const val PORT = 8890
        const val MIME_TYPE = "video/avc"
        const val MIME_TYPE_HEVC = "video/hevc"
        const val TAG = "UScreenVideo"
        const val MAX_FRAME_SIZE = 8 * 1024 * 1024
        const val FRAME_HEADER_SIZE = StreamFramer.FRAME_HEADER_SIZE

        /**
         * Transport deadline. Refreshed by every complete packet, heartbeats
         * included; with a heartbeat-capable host its expiry is a real fault.
         */
        const val READ_TIMEOUT_MS = 10_000
        /** RECOVERING turns into DISCONNECTED (full startup screen) after this long. */
        const val RECOVERY_GRACE_NANOS = 15_000_000_000L
        /** Frames submitted with no output before the decoder is suspected. */
        const val WATCHDOG_MIN_FRAMES = 8
        const val WATCHDOG_STALL_NANOS = 3_000_000_000L
        const val KEYFRAME_REQUEST_MIN_INTERVAL_NANOS = 500_000_000L

        /**
         * Acknowledge every Nth rendered frame.
         *
         * Every frame: an idle screen only sends ~5fps, so sampling one in four
         * left 6-12 measurements per report window and percentiles that moved
         * several milliseconds run to run — enough noise to read a regression
         * into pure variance. One small websocket message per frame is
         * negligible next to the pen event rate.
         */
        const val ACK_EVERY = 1
        const val DISPLAY_REFRESH_HZ = 120f

        /** Frames of arrival history kept for the decode-time split. */
        const val ARRIVAL_RING = 64
    }

    /**
     * What the picture on screen means right now.
     *  WAITING      no frame of this session has been shown yet (startup screen)
     *  STREAMING    frames of the current connection are being rendered
     *  RECOVERING   the video socket dropped after frames were shown; the last
     *               picture stays up while the connection is rebuilt
     *  DISCONNECTED recovery has not succeeded for a while, or stop() was called
     */
    enum class VideoState { WAITING, STREAMING, RECOVERING, DISCONNECTED }

    /** The socket, reader and codec callbacks of one connection attempt. */
    private class Connection(val generation: Int, val socket: Socket)

    @Volatile private var current: Connection? = null
    private val connectionCounter = AtomicInteger(0)
    private var mediaCodec: MediaCodec? = null
    @Volatile private var isRunning = false
    @Volatile private var codecAlive = false

    @Volatile var state = VideoState.DISCONNECTED; private set
    var onStateChanged: ((VideoState) -> Unit)? = null
    var onStatsUpdated: ((decoderFps: Float, receivedMbps: Float) -> Unit)? = null

    /**
     * Whether the host sends heartbeat packets while the desktop is idle
     * (negotiated on the control channel from its greeting). With them, a
     * silent socket is a dead socket; without them silence only means
     * nothing changed, and the last picture must stay.
     */
    @Volatile var hostHeartbeats = false

    /** Sequence of the last frame received / submitted / rendered, for diagnostics. */
    @Volatile private var lastReceivedSeq = -1
    @Volatile private var lastRenderedSeq = -1
    @Volatile private var renderedInConnection = false
    @Volatile private var recoveringSinceNanos = 0L
    @Volatile private var lastKeyframeRequestNanos = 0L
    private var failedAttempts = 0

    /**
     * Decoder watchdog input: frames handed to the codec since the last
     * rendered output, and when that run started. An idle desktop submits
     * nothing, so it never trips the watchdog.
     */
    private val submittedSinceRender = AtomicInteger(0)
    @Volatile private var unrenderedSinceNanos = 0L
    @Volatile private var watchdogStage = 0

    /**
     * Invoked with the host's frame sequence number once that frame is
     * actually on screen. The host times the round trip on its own clock, so
     * no clock synchronisation between the two devices is needed.
     */
    var onFrameRendered: ((seq: Int, decodeUs: Int, renderNanos: Long) -> Unit)? = null

    /** Asks the host for an IDR after this side had to discard dependent frames. */
    var onKeyframeNeeded: (() -> Unit)? = null

    /// Which bitstream the host is sending. Set from the host's greeting
    /// before the stream starts; the frames carry nothing that says which
    /// codec they are, so guessing wrong means a decoder that never outputs.
    @Volatile var mimeType: String = MIME_TYPE_HEVC

    /// Session token; written as the first 64 bytes on the socket. The host
    /// sends nothing until it has seen it.
    @Volatile var token: String? = null

    private var frameCallbackThread: HandlerThread? = null
    private val renderedCount = AtomicLong(0)

    /**
     * seq → nanoTime the frame finished arriving, so the render callback can
     * report how much of the end-to-end latency was spent on this device
     * rather than on the wire. Bounded and cheap: a plain ring, since frames
     * are rendered in the order they arrive.
     */
    private val arrivalSeq = IntArray(ARRIVAL_RING)
    private val arrivalNanos = LongArray(ARRIVAL_RING)
    @Volatile private var arrivalWrite = 0

    /**
     * Splits the on-device time into "decoder produced the frame" and "the
     * compositor put it on screen". Without this the two are indistinguishable,
     * and they call for completely different fixes — decoder settings versus
     * refresh rate and composition path.
     */
    private val releaseNanos = LongArray(ARRIVAL_RING)
    private var decodeSumUs = 0L
    private var presentSumUs = 0L
    private var splitCount = 0
    private var lastSplitLogNanos = 0L

    private fun noteReleased(seq: Int) {
        for (n in 0 until ARRIVAL_RING) {
            val i = (arrivalWrite - 1 - n + ARRIVAL_RING * 2) % ARRIVAL_RING
            if (arrivalSeq[i] == seq) {
                releaseNanos[i] = System.nanoTime()
                return
            }
        }
    }

    private fun noteArrival(seq: Int) {
        val i = arrivalWrite % ARRIVAL_RING
        arrivalSeq[i] = seq
        arrivalNanos[i] = System.nanoTime()
        arrivalWrite = arrivalWrite + 1
    }

    /** Microseconds between the frame arriving and it being on screen, or -1. */
    private fun decodeMicrosFor(seq: Int): Int {
        for (n in 0 until ARRIVAL_RING) {
            val i = (arrivalWrite - 1 - n + ARRIVAL_RING * 2) % ARRIVAL_RING
            if (arrivalSeq[i] == seq && arrivalNanos[i] != 0L) {
                val now = System.nanoTime()
                val total = ((now - arrivalNanos[i]) / 1000L)
                    .coerceIn(0L, Int.MAX_VALUE.toLong()).toInt()

                // Attribute the time: decode = arrival → buffer released,
                // present = released → actually on screen (composition+vsync).
                val rel = releaseNanos[i]
                if (rel > arrivalNanos[i]) {
                    decodeSumUs += (rel - arrivalNanos[i]) / 1000L
                    presentSumUs += (now - rel) / 1000L
                    splitCount++
                    if (now - lastSplitLogNanos > 5_000_000_000L && splitCount > 0) {
                        Log.i(
                            TAG,
                            "on-device split: decode ${decodeSumUs / splitCount / 1000.0}ms " +
                                "present ${presentSumUs / splitCount / 1000.0}ms " +
                                "($splitCount frames)"
                        )
                        lastSplitLogNanos = now
                        decodeSumUs = 0; presentSumUs = 0; splitCount = 0
                    }
                }
                return total
            }
        }
        return -1
    }

    /** Initial decoder format hint; the decoder adapts to the SPS anyway. */
    @Volatile var formatWidth = 2960
    @Volatile var formatHeight = 1848

    /** Frame rate the host is configured to send, used to size decoder hints. */
    @Volatile var streamFps = Prefs.DEFAULT_FPS

    // Stats
    private val frameCounter = AtomicInteger(0)
    private val byteCounter = AtomicLong(0)
    @Volatile var currentFps = 0f; private set
    @Volatile var currentMbps = 0f; private set

    private val surfaceReady = AtomicBoolean(false)
    private val pendingSurface = AtomicReference<Surface?>(null)

    /**
     * Recreated on every [start].
     *
     * These must NOT be `val`s initialised once: [stop] cancels the job, and a
     * cancelled [SupervisorJob] stays cancelled forever, so every later
     * `scope.launch {}` returns an already-dead coroutine whose body never
     * runs. That is what left the tablet on a black screen after the app had
     * been backgrounded once — the only cure was force-stopping it.
     */
    private var job: Job? = null
    private var scope: CoroutineScope? = null

    /**
     * Apply the authenticated host greeting to the decoder. The host owns the
     * real stream format; preferences are only future requests. Rebuild both
     * codec and socket when a live value changes so MediaCodec never keeps a
     * stale frame-rate or dimension hint.
     */
    fun configureStream(config: HostStreamConfig) {
        val wantedMime = if (config.codec.equals("hevc", ignoreCase = true)) {
            MIME_TYPE_HEVC
        } else {
            MIME_TYPE
        }
        val changed = synchronized(this) {
            val differs = mimeType != wantedMime || formatWidth != config.width ||
                formatHeight != config.height || streamFps != config.fps
            mimeType = wantedMime
            formatWidth = config.width
            formatHeight = config.height
            streamFps = config.fps
            differs
        }
        if (!changed) return

        Log.i(
            TAG,
            "Applying host format: $mimeType ${formatWidth}x${formatHeight} @ $streamFps fps"
        )
        val wasRunning = isRunning
        if (wasRunning) {
            stop()
            start()
        } else {
            releaseCodec()
            val surface = pendingSurface.get()
            if (surface != null && surface.isValid) setupCodec(surface)
        }
    }

    fun setSurface(surfaceView: SurfaceView) {
        val surface = surfaceView.holder.surface
        if (surface == null || !surface.isValid) {
            Log.w(TAG, "Surface not ready yet")
            return
        }
        pendingSurface.set(surface)
        surfaceReady.set(true)
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            try {
                surface.setFrameRate(
                    DISPLAY_REFRESH_HZ,
                    Surface.FRAME_RATE_COMPATIBILITY_FIXED_SOURCE
                )
            } catch (e: Exception) {
                Log.w(TAG, "Could not request 120Hz for the video surface: ${e.message}")
            }
        }
        Log.i(TAG, "Surface stored, ready for codec setup")

        synchronized(this) {
            if (mediaCodec == null && surfaceReady.get()) {
                setupCodec(surface)
            }
        }
    }

    /**
     * The surface backing the decoder is going away. Release the codec here
     * rather than letting it keep rendering into a destroyed surface, which
     * throws from the render thread on the way to the background.
     */
    fun onSurfaceDestroyed() {
        surfaceReady.set(false)
        pendingSurface.set(null)
        releaseCodec()
    }

    /**
     * Decoder ownership. One HandlerThread ("uscreen-codec") owns the
     * MediaCodec lifecycle and every input submission; the network reader
     * only appends access units to [inputQueue]. MediaCodec runs in
     * asynchronous mode, so no thread ever polls dequeueInputBuffer with a
     * timeout (the old loop could sit 200 ms waiting for an input buffer and
     * then reset the codec).
     */
    private class AccessUnit(
        val data: ByteArray, val size: Int, val isConfig: Boolean,
        val keyframe: Boolean, val seq: Int, val arrivalNanos: Long
    )

    private var codecThread: HandlerThread? = null
    private var codecHandler: Handler? = null
    private val inputQueue = ArrayDeque<AccessUnit>()
    private val freeInputs = ArrayDeque<Int>()
    private val queueLock = Object()
    @Volatile private var waitingForKeyframe = false
    private var codecGeneration = 0
    private var droppedForAge = 0L
    private var lastDropLogNanos = 0L

    /** Media timestamp (arrival on this device, µs) -> host sequence. Bounded ring. */
    private val ptsSeq = LongArray(ARRIVAL_RING)
    private val ptsSeqValue = IntArray(ARRIVAL_RING)
    private var ptsWrite = 0
    private fun rememberPts(pts: Long, seq: Int) {
        val i = ptsWrite % ARRIVAL_RING
        ptsSeq[i] = pts; ptsSeqValue[i] = seq; ptsWrite++
    }
    private fun seqForPts(pts: Long): Int {
        for (n in 0 until ARRIVAL_RING) {
            val i = (ptsWrite - 1 - n + ARRIVAL_RING * 2) % ARRIVAL_RING
            if (ptsSeq[i] == pts) return ptsSeqValue[i]
        }
        return -1
    }

    /** Two frame periods at the stream rate: older compressed data is stale. */
    private fun queueAgeBudgetNanos(): Long = 2_000_000_000L / streamFps.coerceAtLeast(1)

    private fun setupCodec(surface: Surface): Boolean {
        try {
            val format = MediaFormat.createVideoFormat(mimeType, formatWidth, formatHeight)
            // Follow the stream's real frame rate rather than a hardcoded
            // guess: telling the decoder 90 when the host sends 60 skews its
            // internal pacing and power/clock decisions.
            format.setInteger(MediaFormat.KEY_FRAME_RATE, streamFps)
            format.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)
            try {
                format.setInteger(MediaFormat.KEY_COLOR_RANGE, MediaFormat.COLOR_RANGE_LIMITED)
                format.setInteger(MediaFormat.KEY_COLOR_STANDARD, MediaFormat.COLOR_STANDARD_BT709)
                format.setInteger(MediaFormat.KEY_COLOR_TRANSFER, MediaFormat.COLOR_TRANSFER_SDR_VIDEO)
            } catch (_: Exception) {}

            // Pick the hardware decoder explicitly and only ask for features it
            // declares; a hint the codec does not support is not a setting.
            val codecName = MediaCodecList(MediaCodecList.REGULAR_CODECS).findDecoderForFormat(
                MediaFormat.createVideoFormat(mimeType, formatWidth, formatHeight))
            val codec = if (codecName != null) MediaCodec.createByCodecName(codecName)
                        else MediaCodec.createDecoderByType(mimeType)
            val caps = try { codec.codecInfo.getCapabilitiesForType(mimeType) } catch (_: Exception) { null }
            val lowLatency = android.os.Build.VERSION.SDK_INT >= 30 &&
                caps?.isFeatureSupported(MediaCodecInfo.CodecCapabilities.FEATURE_LowLatency) == true
            if (lowLatency) format.setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
            try {
                format.setFloat(MediaFormat.KEY_OPERATING_RATE, streamFps.toFloat())
            } catch (_: Exception) {}
            val qualcomm = codec.name.contains("qti", ignoreCase = true) ||
                codec.name.contains("qcom", ignoreCase = true)
            if (qualcomm) {
                try { format.setInteger("vendor.qti-ext-dec-low-latency.enable", 1) } catch (_: Exception) {}
            }

            val generation = ++codecGeneration
            val thread = HandlerThread("uscreen-codec").apply { start() }
            val handler = Handler(thread.looper)
            codecThread = thread
            codecHandler = handler
            synchronized(queueLock) { inputQueue.clear(); freeInputs.clear() }
            waitingForKeyframe = true

            codec.setCallback(object : MediaCodec.Callback() {
                override fun onInputBufferAvailable(c: MediaCodec, index: Int) {
                    if (generation != codecGeneration) return
                    synchronized(queueLock) { freeInputs.addLast(index) }
                    submitPending(c, generation)
                }

                override fun onOutputBufferAvailable(c: MediaCodec, index: Int, info: MediaCodec.BufferInfo) {
                    if (generation != codecGeneration) return
                    try {
                        // Render immediately: with a SurfaceView the frame goes
                        // straight to the compositor and OnFrameRendered says
                        // when it was actually shown.
                        c.releaseOutputBuffer(index, true)
                        noteReleased(seqForPts(info.presentationTimeUs))
                        frameCounter.incrementAndGet()
                    } catch (e: IllegalStateException) {
                        Log.w(TAG, "Output release: codec gone", e)
                    }
                }

                override fun onError(c: MediaCodec, e: MediaCodec.CodecException) {
                    Log.e(TAG, "Decoder error: ${e.diagnosticInfo}", e)
                    if (generation == codecGeneration) handler.post { recoverDecoder("codec error") }
                }

                override fun onOutputFormatChanged(c: MediaCodec, f: MediaFormat) {
                    Log.i(TAG, "Decoder output format: $f")
                }
            }, handler)

            codec.configure(format, surface, null, 0)
            codec.setVideoScalingMode(MediaCodec.VIDEO_SCALING_MODE_SCALE_TO_FIT)
            val applied = try { codec.inputFormat } catch (_: Exception) { null }
            val appliedLowLatency = applied?.containsKey(MediaFormat.KEY_LOW_LATENCY) == true &&
                applied.getInteger(MediaFormat.KEY_LOW_LATENCY) == 1
            val appliedVendor = applied?.containsKey("vendor.qti-ext-dec-low-latency.enable") == true

            // Fires when a frame has actually reached the output surface —
            // the true "it is on screen" moment. The frame is identified
            // through its media timestamp (arrival time on this device).
            val cbThread = HandlerThread("uscreen-frame-cb").apply { start() }
            frameCallbackThread = cbThread
            codec.setOnFrameRenderedListener({ _, presentationTimeUs, nanoTime ->
                submittedSinceRender.set(0)
                watchdogStage = 0
                val seq = seqForPts(presentationTimeUs)
                if (seq >= 0) lastRenderedSeq = seq
                if (!renderedInConnection) {
                    // "Streaming" means a frame of this connection is on screen,
                    // not that bytes arrived.
                    renderedInConnection = true
                    failedAttempts = 0
                    setState(VideoState.STREAMING)
                }
                if (renderedCount.incrementAndGet() % ACK_EVERY == 0L) {
                    if (seq >= 0) onFrameRendered?.invoke(seq, decodeMicrosFor(seq), nanoTime)
                }
            }, Handler(cbThread.looper))

            codec.start()
            mediaCodec = codec
            codecAlive = true
            Log.i(
                TAG,
                "Codec ${codec.name} configured: $mimeType ${formatWidth}x${formatHeight} @ $streamFps fps, " +
                    "low-latency feature=${lowLatency} applied=${appliedLowLatency}, " +
                    "qti hint applied=${appliedVendor}, input format=${applied}"
            )
            return true
        } catch (e: Exception) {
            Log.e(TAG, "Failed to setup codec", e)
            return false
        }
    }

    /**
     * Network reader side: append one access unit. Bounded by age as well as
     * count: if the oldest queued unit is older than two frame periods the
     * compressed stream is behind, so the whole generation is discarded and
     * decoding resumes at the next keyframe (never a dependent frame without
     * its references). The host is asked for an IDR so that wait is short.
     */
    private fun enqueueAccessUnit(codec: MediaCodec, unit: AccessUnit) {
        val generation = codecGeneration
        synchronized(queueLock) {
            if (unit.isConfig) {
                inputQueue.addLast(unit)
            } else {
                val oldest = inputQueue.firstOrNull { !it.isConfig }
                val stale = oldest != null && unit.arrivalNanos - oldest.arrivalNanos > queueAgeBudgetNanos()
                if (stale || inputQueue.size >= 8) {
                    inputQueue.removeAll { !it.isConfig }
                    waitingForKeyframe = true
                    droppedForAge++
                    if (unit.arrivalNanos - lastDropLogNanos > 1_000_000_000L) {
                        lastDropLogNanos = unit.arrivalNanos
                        Log.w(TAG, "Compressed backlog over budget; resuming at the next keyframe (total $droppedForAge)")
                    }
                    requestKeyframe("compressed backlog discarded")
                }
                if (waitingForKeyframe && !unit.keyframe) {
                    return
                }
                waitingForKeyframe = false
                inputQueue.addLast(unit)
            }
        }
        codecHandler?.post { submitPending(codec, generation) }
    }

    /** Codec thread: marry queued access units with free input buffers. */
    private fun submitPending(codec: MediaCodec, generation: Int) {
        while (true) {
            val unit: AccessUnit
            val index: Int
            synchronized(queueLock) {
                if (generation != codecGeneration || inputQueue.isEmpty() || freeInputs.isEmpty()) return
                unit = inputQueue.removeFirst()
                index = freeInputs.removeFirst()
            }
            try {
                val buffer = codec.getInputBuffer(index) ?: return
                buffer.clear()
                buffer.put(unit.data, 0, unit.size)
                val flags = if (unit.isConfig) MediaCodec.BUFFER_FLAG_CODEC_CONFIG else 0
                val pts = unit.arrivalNanos / 1000L
                if (!unit.isConfig) {
                    rememberPts(pts, unit.seq)
                    if (submittedSinceRender.getAndIncrement() == 0) unrenderedSinceNanos = System.nanoTime()
                }
                codec.queueInputBuffer(index, 0, unit.size, pts, flags)
            } catch (e: IllegalStateException) {
                Log.w(TAG, "Submit: codec gone", e)
                return
            }
        }
    }

    fun start() {
        synchronized(this) {
            if (isRunning) return
            isRunning = true
            // Fresh job/scope per start — see the field docs.
            val newJob = SupervisorJob()
            val newScope = CoroutineScope(Dispatchers.IO + newJob)
            job = newJob
            scope = newScope

            setState(VideoState.WAITING)
            newScope.launch {
                connectAndReceive()
            }

            newScope.launch {
                while (isRunning) {
                    delay(1000)
                    watchdogTick()
                }
            }

            newScope.launch {
                var previousNanos = android.os.SystemClock.elapsedRealtimeNanos()
                while (isRunning) {
                    delay(1000)
                    val now = android.os.SystemClock.elapsedRealtimeNanos()
                    val seconds = ((now - previousNanos) / 1_000_000_000.0)
                        .coerceAtLeast(0.001)
                    previousNanos = now
                    currentFps = (frameCounter.getAndSet(0) / seconds).toFloat()
                    currentMbps = (byteCounter.getAndSet(0) * 8.0 /
                        1_000_000.0 / seconds).toFloat()
                    onStatsUpdated?.invoke(currentFps, currentMbps)
                }
            }
        }
    }

    private suspend fun connectAndReceive() {
        while (isRunning) {
            var connection: Connection? = null
            var reason = "stopped"
            try {
                // Wait for surface to be ready before connecting
                while (isRunning && !surfaceReady.get()) {
                    Log.d(TAG, "Waiting for surface...")
                    delay(200)
                }
                if (!isRunning) return

                // Ensure codec is set up
                val codecReady = synchronized(this@VideoReceiver) {
                    if (mediaCodec == null) {
                        val surface = pendingSurface.get()
                        if (surface != null && surface.isValid) {
                            setupCodec(surface)
                        } else {
                            false
                        }
                    } else {
                        true
                    }
                }
                if (!codecReady) {
                    Log.w(TAG, "Codec/surface not ready, retrying...")
                    delay(500)
                    continue
                }

                val generation = connectionCounter.incrementAndGet()
                Log.i(TAG, "Video connection $generation: connecting to $HOST:$PORT...")
                val socket = Socket(HOST, PORT).apply {
                    tcpNoDelay = true
                    soTimeout = READ_TIMEOUT_MS
                    // Small on purpose. A 1 MB receive buffer let the host run
                    // ahead and park whole frames here, where they are pure
                    // delay that neither side can see or skip past. Keeping it
                    // shallow pushes backpressure back to the host, which does
                    // know how to drop stale frames.
                    receiveBufferSize = 128 * 1024
                }
                connection = Connection(generation, socket)
                if (!isRunning) { socket.close(); return }
                current = connection
                token?.let { t ->
                    socket.getOutputStream().apply {
                        write(t.toByteArray(Charsets.US_ASCII))
                        flush()
                    }
                }
                // Every connection starts at an IDR: whatever the decoder held
                // belongs to a stream it will never see the rest of.
                synchronized(queueLock) { inputQueue.removeAll { !it.isConfig }; waitingForKeyframe = true }
                renderedInConnection = false
                requestKeyframe("connection $generation")
                Log.i(TAG, "Video connection $generation: connected, waiting for a keyframe " +
                    "(host heartbeats: $hostHeartbeats)")

                val framer = StreamFramer(socket.getInputStream(), MAX_FRAME_SIZE)
                var idleLogged = false
                val connectedNanos = System.nanoTime()

                receiveLoop@ while (isRunning && current === connection) {
                    // A decoder rebuild (error or watchdog) briefly leaves no
                    // codec; the socket is kept so the stream resumes at the
                    // IDR the rebuild asked for, without a reconnect.
                    var codec = mediaCodec
                    var waitedMs = 0
                    while (codec == null && isRunning && current === connection && waitedMs < 2000) {
                        delay(50); waitedMs += 50; codec = mediaCodec
                    }
                    if (codec == null) { reason = "no decoder for 2 s"; break@receiveLoop }
                    val packet = try {
                        framer.next()
                    } catch (e: java.net.SocketTimeoutException) {
                        if (hostHeartbeats) {
                            reason = "no packet for ${READ_TIMEOUT_MS / 1000} s (host heartbeats expected)"
                            break@receiveLoop
                        }
                        // Legacy host: silence is an idle desktop, not a fault. A dead
                        // peer on this loopback link shows up as EOF or a reset.
                        if (!idleLogged) {
                            idleLogged = true
                            Log.i(TAG, "Video connection $generation: idle for ${READ_TIMEOUT_MS / 1000} s, " +
                                "host sends no heartbeat; keeping the connection")
                        }
                        continue@receiveLoop
                    }
                    byteCounter.addAndGet(packet.size.toLong())

                    when (packet.type) {
                        StreamFramer.TYPE_HEARTBEAT -> {
                            // Liveness only: not a frame, not decoded, not counted.
                        }
                        StreamFramer.TYPE_CONFIG -> {
                            val payloadSize = packet.size - 1
                            Log.i(TAG, "Received codec config: ${payloadSize}B")
                            enqueueAccessUnit(codec, AccessUnit(packet.buffer.copyOfRange(1, 1 + payloadSize),
                                payloadSize, true, true, -1, System.nanoTime()))
                        }
                        StreamFramer.TYPE_FRAME -> {
                            val seq = packet.seq()
                            lastReceivedSeq = seq
                            noteArrival(seq)
                            val size = packet.size - FRAME_HEADER_SIZE
                            enqueueAccessUnit(codec, AccessUnit(
                                packet.buffer.copyOfRange(FRAME_HEADER_SIZE, FRAME_HEADER_SIZE + size),
                                size, false, isKeyframe(packet.buffer, FRAME_HEADER_SIZE, size),
                                seq, System.nanoTime()))
                        }
                    }
                }
                if (current !== connection) reason = "superseded"
                Log.i(TAG, "Video connection $generation: ended after " +
                    "${(System.nanoTime() - connectedNanos) / 1_000_000_000} s, " +
                    "${framer.packets} packets, last seq received $lastReceivedSeq rendered $lastRenderedSeq")
            } catch (e: java.io.EOFException) {
                reason = "host closed the stream"
            } catch (e: FramingException) {
                reason = "framing: ${e.message}"
            } catch (e: Exception) {
                reason = if (isRunning && current === connection) "error: ${e.javaClass.simpleName}: ${e.message}"
                         else "closed by stop()"
            } finally {
                try { connection?.socket?.close() } catch (_: Exception) {}
                if (current === connection) current = null
            }
            if (!isRunning) return
            onConnectionLost(reason)
            delay(reconnectDelayMs())
        }
    }

    /** Called on the network coroutine when a connection ends for any reason. */
    private fun onConnectionLost(reason: String) {
        val cameFromStreaming = state == VideoState.STREAMING
        Log.w(TAG, "Video connection lost: $reason")
        if (cameFromStreaming) {
            recoveringSinceNanos = System.nanoTime()
            setState(VideoState.RECOVERING)
        } else if (state == VideoState.RECOVERING &&
                   System.nanoTime() - recoveringSinceNanos > RECOVERY_GRACE_NANOS) {
            setState(VideoState.DISCONNECTED)
        }
        failedAttempts++
    }

    /** 500 ms, 1 s, 2 s, 4 s, then 4 s: quick first retry, no reconnect storm. */
    private fun reconnectDelayMs(): Long = (500L shl (failedAttempts - 1).coerceIn(0, 3))

    private fun setState(next: VideoState) {
        val previous = state
        if (previous == next) return
        state = next
        Log.i(TAG, "Video state: $previous -> $next (connection ${connectionCounter.get()})")
        onStateChanged?.invoke(next)
    }

    /** One IDR request per half second at most, whatever the trigger. */
    private fun requestKeyframe(why: String) {
        val now = System.nanoTime()
        if (now - lastKeyframeRequestNanos < KEYFRAME_REQUEST_MIN_INTERVAL_NANOS) return
        lastKeyframeRequestNanos = now
        Log.i(TAG, "Requesting a keyframe: $why")
        onKeyframeNeeded?.invoke()
    }

    /**
     * Frames keep going into the decoder but nothing comes out: first ask for
     * an IDR (a lost reference is the common case), then rebuild the decoder
     * once, then rebuild the connection. Never fires on an idle desktop, where
     * nothing is submitted.
     */
    private fun watchdogTick() {
        val pending = submittedSinceRender.get()
        if (pending < WATCHDOG_MIN_FRAMES) return
        val stalledFor = System.nanoTime() - unrenderedSinceNanos
        if (stalledFor < WATCHDOG_STALL_NANOS * (watchdogStage + 1)) return
        when (watchdogStage) {
            0 -> { watchdogStage = 1; requestKeyframe("decoder produced nothing for $pending frames") }
            1 -> { watchdogStage = 2; codecHandler?.post { recoverDecoder("no output for $pending frames after a keyframe request") } }
            else -> { watchdogStage = 3; dropConnection("decoder still silent after a reset") }
        }
    }

    /** Serialized on the codec thread: rebuild the decoder and resume at an IDR. */
    private fun recoverDecoder(why: String) {
        Log.w(TAG, "Rebuilding the decoder: $why")
        resetCodec()
        submittedSinceRender.set(0)
        requestKeyframe("decoder rebuilt")
    }

    /** Close the live socket; the network coroutine reconnects with backoff. */
    private fun dropConnection(why: String) {
        val connection = current ?: return
        Log.w(TAG, "Dropping video connection ${connection.generation}: $why")
        try { connection.socket.close() } catch (_: Exception) {}
    }

    /** Recovery drills (debug builds): behave exactly like the real faults. */
    fun debugDropSocket() = dropConnection("debug drill")
    fun debugResetDecoder() { codecHandler?.post { recoverDecoder("debug drill") } ?: Log.w(TAG, "No decoder to reset") }

    /**
     * Whether this access unit starts with (or contains) an IDR/I slice, so
     * decoding can resume here after a discard. Annex B byte stream; HEVC
     * NAL type bits are in the first header byte, H.264 in its low 5 bits.
     */
    private fun isKeyframe(data: ByteArray, offset: Int, size: Int): Boolean {
        var i = offset
        val end = offset + size - 4
        val hevc = mimeType == MIME_TYPE_HEVC
        while (i < end) {
            if (data[i].toInt() == 0 && data[i + 1].toInt() == 0 && data[i + 2].toInt() == 1) {
                val header = data[i + 3].toInt() and 0xFF
                if (hevc) {
                    val type = (header shr 1) and 0x3F
                    if (type in 16..21) return true          // BLA/IDR/CRA
                    if (type == 32 || type == 33 || type == 34) { i += 3; continue } // VPS/SPS/PPS
                } else {
                    val type = header and 0x1F
                    if (type == 5) return true
                    if (type == 7 || type == 8) { i += 3; continue }
                }
                i += 3
            } else {
                i++
            }
        }
        return false
    }

    /** Tear the decoder down without touching the surface or the socket. */
    private fun releaseCodec() {
        synchronized(this) {
            codecAlive = false
            codecGeneration++          // callbacks from the old codec are ignored
            synchronized(queueLock) { inputQueue.clear(); freeInputs.clear() }
            mediaCodec?.let {
                try { it.stop() } catch (_: Exception) {}
                try { it.release() } catch (_: Exception) {}
            }
            mediaCodec = null
            codecThread?.quitSafely()
            codecThread = null
            codecHandler = null
            frameCallbackThread?.quitSafely()
            frameCallbackThread = null
        }
    }

    private fun resetCodec() {
        synchronized(this) {
            releaseCodec()
            val surface = pendingSurface.get()
            if (surface != null && surface.isValid) {
                setupCodec(surface)
            }
        }
    }

    fun getFps(): Float = currentFps
    fun getMbps(): Float = currentMbps

    fun stop() {
        isRunning = false
        codecAlive = false
        // Close the live socket first to unblock any pending read. Only the
        // current connection is touched: a later start() owns its own.
        val connection = current
        current = null
        try { connection?.socket?.close() } catch (_: Exception) {}
        setState(VideoState.DISCONNECTED)

        // Then cancel coroutines. The job is dropped rather than reused: a new
        // one is created by the next start().
        job?.cancel()
        job = null
        scope = null

        releaseCodec()

        // The surface is deliberately left alone. It belongs to the
        // SurfaceView, which outlives any single streaming session — it stays
        // in the view tree while the tablet is a graphics tablet, so no
        // surfaceCreated callback ever comes to hand it back. Clearing it here
        // left the next start() waiting on a surface that would never arrive,
        // showing the last decoded frame frozen on screen.
    }
}
