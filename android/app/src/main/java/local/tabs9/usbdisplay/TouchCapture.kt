package local.tabs9.usbdisplay

import android.util.Base64
import android.util.Log
import android.view.MotionEvent
import android.view.SurfaceView
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.sin
import kotlinx.coroutines.*
import okhttp3.*
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

data class HostStreamConfig(
    val codec: String = "hevc",
    val width: Int = 2960,
    val height: Int = 1848,
    val fps: Int = Prefs.DEFAULT_FPS,
    val bitrateKbps: Int = Prefs.DEFAULT_BITRATE_KBPS,
)

class TouchCapture {
    companion object {
        const val TAG = "tabs9Touch"
        /** Control-channel contract this client implements (see host.py PROTOCOL). */
        const val PROTOCOL = 2
        /** Optional capabilities this client implements; the host enables each only when named here. */
        val FEATURES = listOf("video_heartbeat")
        const val WS_URL = "ws://127.0.0.1:8891"
        /** Clip bytes per control message: base64 of 2400 is 3200 characters, inside the host's 4 KiB frame limit. */
        private const val CLIP_CHUNK = 2400
        private const val TOOL_TYPE_PALM = 6
        const val RECONNECT_DELAY_MS = 2000L
    }

    private var webSocket: WebSocket? = null
    @Volatile private var isConnected = false
    /**
     * True between connect() and disconnect(). A socket closed on purpose
     * still reports onClosed, which used to schedule a reconnect: the tablet
     * then reconnected from the background two seconds after the activity
     * stopped, and that ghost socket kept the host's control channel for as
     * long as the process lived.
     */
    @Volatile private var wanted = false
    private var reconnectJob: Job? = null
    private var surfaceView: SurfaceView? = null

    /** Set from the host's greeting: it is using us as a graphics tablet for
     *  its own screen, so no video will arrive and none should be waited for. */
    @Volatile var isPenOnly = false
        private set
    var onModeKnown: ((penOnly: Boolean) -> Unit)? = null
    var onStreamConfigKnown: ((HostStreamConfig) -> Unit)? = null
    /** Host protocol version from its greeting (1 when it sends none). */
    var onProtocolKnown: ((Int) -> Unit)? = null
    /** Feature names the host listed in its greeting (empty for a legacy host). */
    var onHostFeaturesKnown: ((Set<String>) -> Unit)? = null

    /**
     * While the picture is known to be unavailable (video recovering) no new
     * gesture is forwarded: the user cannot see what they would be touching.
     * Contacts that were down when it started are lifted so nothing stays
     * pressed on the desktop.
     */
    @Volatile private var inputSuspended = false
    private val activeTouches = HashMap<Int, Pair<Float, Float>>()
    private var penContact: Pair<Double, Double>? = null
    /** Slots lifted by a suspension: their gesture is over for the host; ignore it until a new down. */
    private val cancelledSlots = HashSet<Int>()
    private var penCancelled = false
    private val contactLock = Object()

    @Volatile var hostStreamConfig = HostStreamConfig()
        private set

    /// Session token from the host, delivered as an intent extra when the
    /// daemon launches us over adb. Must be the first thing sent on the
    /// socket; without it the host closes the connection unanswered.
    @Volatile var token: String? = null

    /** Settings to (re)send to the host whenever the control channel connects */
    @Volatile private var pendingConfig: JSONObject? = null
    @Volatile private var pendingMode: JSONObject? = null

    /** Tablet's native landscape resolution, reported to the host on connect
     *  so the virtual display can match it automatically. */
    @Volatile private var nativeWidth = 0
    @Volatile private var nativeHeight = 0

    /** Physical panel size in millimetres, so the host can build an EDID that
     *  reports the true DPI and the desktop comes up at a sane scale. */
    @Volatile private var nativeWidthMm = 0
    @Volatile private var nativeHeightMm = 0

    fun setNativeResolution(width: Int, height: Int, widthMm: Int = 0, heightMm: Int = 0) {
        nativeWidth = width
        nativeHeight = height
        nativeWidthMm = widthMm
        nativeHeightMm = heightMm
    }

    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.SECONDS)
        .connectTimeout(5, TimeUnit.SECONDS)
        .build()

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    private val wsListener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            isConnected = true
            Log.i(TAG, "Connected")
            // Authenticate before anything else. If we have no token yet the
            // host will drop us and relaunch the app with one, and the
            // reconnect logic takes it from there.
            token?.let { t ->
                webSocket.send(JSONObject().apply {
                    put("type", "auth")
                    put("token", t)
                }.toString())
            } ?: Log.w(TAG, "No session token yet — the host will send one")
            if (nativeWidth > 0 && nativeHeight > 0) {
                val res = JSONObject().apply {
                    put("type", "resolution")
                    put("width", nativeWidth)
                    put("height", nativeHeight)
                    if (nativeWidthMm > 0 && nativeHeightMm > 0) {
                        put("width_mm", nativeWidthMm)
                        put("height_mm", nativeHeightMm)
                    }
                }
                webSocket.send(res.toString())
                Log.i(TAG, "Reported native resolution: ${nativeWidth}x${nativeHeight} " +
                        "(${nativeWidthMm}x${nativeHeightMm} mm)")
            }
            // Always say which protocol and features this client speaks. A
            // fresh install has no user settings, so pendingConfig is null and
            // the host would otherwise never learn that we want heartbeats:
            // every idle desktop then looked like a dead link after 10 s.
            // This hello carries no fps/bitrate, so the host's own profile
            // stays in force.
            webSocket.send((pendingConfig ?: JSONObject().apply {
                put("type", "config")
                put("protocol", PROTOCOL)
                put("features", JSONArray(FEATURES))
            }).toString())
            pendingMode?.let {
                webSocket.send(it.toString())
                pendingMode = null
            }
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            // The host greets with its mode and answers clipboard transfers;
            // everything else it might say is ignored, this channel is
            // otherwise ours to talk on.
            try {
                val o = JSONObject(text)
                if (o.optString("type") == "clip_ack") {
                    finishClip(o.optInt("id", -1),
                        if (o.optBoolean("ok", false)) null else o.optString("error", "The host refused the clip"))
                    return
                }
                if (o.has("features")) {
                    val list = o.optJSONArray("features")
                    val names = HashSet<String>()
                    if (list != null) for (i in 0 until list.length()) list.optString(i, null)?.let { names.add(it) }
                    onHostFeaturesKnown?.invoke(names)
                } else if (o.has("status")) {
                    onHostFeaturesKnown?.invoke(emptySet())
                }
                if (o.has("protocol")) {
                    val hostProtocol = o.optInt("protocol", 1)
                    if (hostProtocol != PROTOCOL) {
                        Log.w(TAG, "Host speaks protocol $hostProtocol, this client $PROTOCOL; " +
                            "using the common subset")
                    }
                    onProtocolKnown?.invoke(hostProtocol)
                } else if (o.has("status")) {
                    Log.w(TAG, "Host greeting has no protocol version (legacy host)")
                    onProtocolKnown?.invoke(1)
                }
                val previous = hostStreamConfig
                val updated = HostStreamConfig(
                    codec = o.optString("codec", previous.codec),
                    width = o.optInt("width", previous.width).coerceIn(320, 8192),
                    height = o.optInt("height", previous.height).coerceIn(240, 8192),
                    fps = o.optInt("fps", previous.fps).coerceIn(1, 240),
                    bitrateKbps = o.optInt("bitrate", previous.bitrateKbps)
                        .coerceIn(Prefs.MIN_BITRATE_KBPS, Prefs.MAX_BITRATE_KBPS),
                )
                if (updated != previous) {
                    hostStreamConfig = updated
                    Log.i(
                        TAG,
                        "Host stream: ${updated.codec} ${updated.width}x${updated.height} " +
                            "@ ${updated.fps} fps, ${updated.bitrateKbps} kbps"
                    )
                }
                if (o.has("codec") || o.has("width") || o.has("height") ||
                    o.has("fps") || o.has("bitrate")) {
                    onStreamConfigKnown?.invoke(updated)
                }
                if (o.has("pen_only")) {
                    val pen = o.getBoolean("pen_only")
                    if (pen != isPenOnly) {
                        isPenOnly = pen
                        Log.i(TAG, "Host mode: ${if (pen) "pen-only" else "display"}")
                    }
                    onModeKnown?.invoke(pen)
                }
            } catch (_: Exception) {}
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(1000, null)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            isConnected = false
            failClips("The connection to the host closed")
            scheduleReconnect()
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            isConnected = false
            Log.w(TAG, "Connection failed: ${t.message}")
            failClips("The connection to the host failed")
            scheduleReconnect()
        }
    }

    // The surface only forwards touches to the host; there is no click to perform.
    @android.annotation.SuppressLint("ClickableViewAccessibility")
    fun setSurfaceView(sv: SurfaceView) {
        surfaceView = sv

        sv.setOnTouchListener { view, event ->
            handleMotionEvent(event, view.width, view.height)
            true
        }

        // S-Pen hover: pen near screen moves cursor without clicking.
        // Without this, the first touch always snaps the cursor to the pen
        // position and fires a click simultaneously (jarring).
        sv.setOnHoverListener { view, event ->
            if (!isConnected) return@setOnHoverListener false
            val vw = view.width.coerceAtLeast(1).toFloat()
            val vh = view.height.coerceAtLeast(1).toFloat()
            when (event.actionMasked) {
                MotionEvent.ACTION_HOVER_ENTER,
                MotionEvent.ACTION_HOVER_MOVE -> {
                    if (isPenLike(event, 0)) sendPenEvent(event, 0, 3, vw, vh)
                }
                MotionEvent.ACTION_HOVER_EXIT -> {
                    if (isPenLike(event, 0)) sendPenProximityExit()
                }
            }
            true
        }
    }

    fun connect() {
        // Idempotent: a second connect() must not leave the first socket
        // alive with its listener still flipping isConnected. onStart and a
        // token delivered through onNewIntent can both call this.
        wanted = true
        if (isConnected) return
        // A reconnect already scheduled by onClosed would open a second
        // socket next to this one; this call supersedes it.
        reconnectJob?.cancel()
        webSocket?.cancel()
        webSocket = null
        connectWebSocket()
    }

    private fun connectWebSocket() {
        webSocket?.cancel()
        val request = Request.Builder()
            .url(WS_URL)
            .build()
        webSocket = client.newWebSocket(request, wsListener)
    }

    private fun scheduleReconnect() {
        reconnectJob?.cancel()
        if (!wanted) return
        reconnectJob = scope.launch {
            delay(RECONNECT_DELAY_MS)
            if (wanted && !isConnected) {
                connectWebSocket()
            }
        }
    }

    /**
     * Forward stylus hover so the host's cursor follows the pen before it
     * touches down, and the S Pen's side button. Returns true only for those,
     * so nothing else the activity might want to do with generic motion
     * events is disturbed.
     */
    fun handleHoverEvent(event: MotionEvent, width: Int, height: Int): Boolean {
        if (!isConnected) return false
        val vw = width.coerceAtLeast(1).toFloat()
        val vh = height.coerceAtLeast(1).toFloat()
        return when (event.actionMasked) {
            MotionEvent.ACTION_HOVER_ENTER,
            MotionEvent.ACTION_HOVER_MOVE -> {
                if (!isPenLike(event, 0)) return false
                notePenButtons(event)
                sendPenEvent(event, 0, 3, vw, vh)
                true
            }
            MotionEvent.ACTION_HOVER_EXIT -> {
                if (!isPenLike(event, 0)) return false
                notePenButtons(event)
                sendPenProximityExit()
                true
            }
            MotionEvent.ACTION_BUTTON_PRESS,
            MotionEvent.ACTION_BUTTON_RELEASE -> {
                if (!isPenLike(event, 0)) return false
                notePenButtons(event)
                true
            }
            else -> false
        }
    }

    /** Whether the S Pen's side button was down at the last pen event. */
    private var penButtonDown = false

    /**
     * Report the S Pen's side button from the button state every pen event
     * carries, on the edges only. The discrete ACTION_BUTTON_PRESS/RELEASE
     * cannot be relied on: while the pen is hovering the input dispatcher
     * drops them (no pointer is down, InputDispatcher's "Case 2"), and that
     * is exactly when the host wants the press. The hover move that carries
     * the new state does arrive.
     */
    private fun notePenButtons(event: MotionEvent) {
        val down = event.buttonState and MotionEvent.BUTTON_STYLUS_PRIMARY != 0
        if (down == penButtonDown) return
        penButtonDown = down
        sendPenButton(down)
    }

    fun handleMotionEvent(event: MotionEvent, width: Int, height: Int): Boolean {
        if (!isConnected) return false
        if (inputSuspended) return true
        // A mouse on the tablet is not a finger: while the computer is driving
        // this tablet (remote control), its pointer arrives as mouse events,
        // and forwarding them would send them straight back where they came
        // from. Real fingers and the pen are never TOOL_TYPE_MOUSE.
        if (isMouse(event)) return false

        val vw = width.coerceAtLeast(1).toFloat()
        val vh = height.coerceAtLeast(1).toFloat()

        val pointerCount = event.pointerCount
        val actionIndex = event.actionIndex
        val maskedAction = event.actionMasked

        when (maskedAction) {
            MotionEvent.ACTION_DOWN,
            MotionEvent.ACTION_POINTER_DOWN -> {
                // Drop palm contacts — Samsung sends TOOL_TYPE_PALM for
                // unintentional palm-rest touches; forwarding them causes
                // phantom scrolling on the Linux side.
                if (isPalm(event, actionIndex)) {
                    return true
                }
                if (isPenLike(event, actionIndex)) {
                    notePenButtons(event)
                    sendPenEvent(event, actionIndex, 0, vw, vh)
                } else {
                    sendTouch(event.getX(actionIndex) / vw,
                        event.getY(actionIndex) / vh,
                        event.getPressure(actionIndex).toDouble(),
                        0, slotOf(event, actionIndex))
                }
            }

            MotionEvent.ACTION_MOVE -> {
                for (i in 0 until pointerCount) {
                    if (isPalm(event, i)) continue
                    if (isPenLike(event, i)) {
                        notePenButtons(event)
                        // Android batches several samples between frames.
                        // Forward the historical points too, otherwise fast
                        // pen strokes look jagged in GIMP.
                        val hist = event.historySize
                        for (h in 0 until hist) {
                            val hx = event.getHistoricalX(i, h) / vw
                            val hy = event.getHistoricalY(i, h) / vh
                            val hp = event.getHistoricalPressure(i, h).toDouble()
                            val (htx, hty) = decomposeTilt(
                                getHistoricalAxis(event, MotionEvent.AXIS_TILT, i, h),
                                getHistoricalAxis(event, MotionEvent.AXIS_ORIENTATION, i, h))
                            emitPen(hx.toDouble(), hy.toDouble(), hp, htx, hty,
                                isEraser(event, i), 2)
                        }
                        sendPenEvent(event, i, 2, vw, vh)
                    } else {
                        sendTouch(event.getX(i) / vw,
                            event.getY(i) / vh,
                            event.getPressure(i).toDouble(),
                            2, slotOf(event, i))
                    }
                }
            }

            MotionEvent.ACTION_UP,
            MotionEvent.ACTION_POINTER_UP -> {
                if (isPalm(event, actionIndex)) {
                    return true
                }
                if (isPenLike(event, actionIndex)) {
                    notePenButtons(event)
                    sendPenEvent(event, actionIndex, 1, vw, vh)
                } else {
                    sendTouch(event.getX(actionIndex) / vw,
                        event.getY(actionIndex) / vh,
                        0.0, 1, slotOf(event, actionIndex))
                }
            }

            MotionEvent.ACTION_CANCEL -> {
                // A cancelled pen contact must lift the pen, not a touch slot:
                // the host tracks the two separately and would otherwise keep
                // the pointer button pressed until the socket drops.
                for (i in 0 until pointerCount) {
                    if (isPalm(event, i)) continue
                    if (isPenLike(event, i)) {
                        sendPenEvent(event, i, 1, vw, vh)
                    } else {
                        sendTouch(event.getX(i) / vw,
                            event.getY(i) / vh,
                            0.0, 1, slotOf(event, i))
                    }
                }
            }
        }
        return true
    }

    /**
     * Multitouch slot for a pointer, derived from its stable pointer *id*.
     *
     * The pointer *index* must not be used here: Android repacks indices
     * whenever a finger lifts, so with two fingers down, lifting the first
     * renumbers the second from index 1 to index 0. The host would then see
     * slot 1 released while slot 0 keeps moving under a different finger, and
     * pinch and two-finger scroll come apart. The pointer id stays with the
     * finger for the whole gesture.
     *
     * Clamped to the 10 slots the uinput touchscreen declares.
     */
    private fun slotOf(event: MotionEvent, index: Int): Int {
        return try {
            event.getPointerId(index).coerceIn(0, 9)
        } catch (_: Exception) {
            index.coerceIn(0, 9)
        }
    }

    /** Stylus or its eraser end — both drive the pen/tablet device. */
    private fun isPenLike(event: MotionEvent, index: Int): Boolean {
        return try {
            val t = event.getToolType(index)
            t == MotionEvent.TOOL_TYPE_STYLUS || t == MotionEvent.TOOL_TYPE_ERASER
        } catch (_: Exception) {
            false
        }
    }

    private fun isEraser(event: MotionEvent, index: Int): Boolean {
        return try {
            event.getToolType(index) == MotionEvent.TOOL_TYPE_ERASER
        } catch (_: Exception) {
            false
        }
    }

    private fun getAxis(event: MotionEvent, axis: Int, index: Int): Double {
        return try {
            event.getAxisValue(axis, index).toDouble()
        } catch (_: Exception) {
            0.0
        }
    }

    private fun getHistoricalAxis(event: MotionEvent, axis: Int, index: Int, hist: Int): Double {
        return try {
            event.getHistoricalAxisValue(axis, index, hist).toDouble()
        } catch (_: Exception) {
            0.0
        }
    }

    /**
     * Decompose Android's stylus tilt into X/Y tilt angles, **in degrees**.
     *
     * Android exposes AXIS_TILT as the angle from the screen normal (0 =
     * perpendicular, π/2 = flat) and AXIS_ORIENTATION as the azimuth of the
     * tilt around that normal (0..2π). A Wacom-style ABS_TILT_X/Y device wants
     * the signed X and Y tilt *angles*, which are the arctangents of the tilt
     * vector's components projected onto the surface — not the components
     * themselves.
     *
     * The previous version returned the raw projections `sin(tilt)·cos(orient)`
     * (a dimensionless value in [-1,1]) and the host multiplied them by
     * 180/π as if they were radians. A pen laid flat at 90° came out as 57°,
     * and everything in between was wrong non-linearly.
     */
    private fun decomposeTilt(tiltRad: Double, orientationRad: Double): Pair<Double, Double> {
        val sinTilt = sin(tiltRad)
        val cosTilt = cos(tiltRad)
        val tx = atan2(sinTilt * cos(orientationRad), cosTilt)
        val ty = atan2(sinTilt * sin(orientationRad), cosTilt)
        return Math.toDegrees(tx) to Math.toDegrees(ty)
    }

    private fun sendPenEvent(event: MotionEvent, index: Int, action: Int,
                              vw: Float, vh: Float) {
        val x = event.getX(index) / vw
        val y = event.getY(index) / vh
        val pressure = event.getPressure(index).toDouble()
        val (tiltX, tiltY) = decomposeTilt(
            getAxis(event, MotionEvent.AXIS_TILT, index),
            getAxis(event, MotionEvent.AXIS_ORIENTATION, index))
        emitPen(x.toDouble(), y.toDouble(), pressure, tiltX, tiltY,
            isEraser(event, index), action)
    }

    private fun emitPen(x: Double, y: Double, pressure: Double,
                        tiltX: Double, tiltY: Double, eraser: Boolean, action: Int) {
        val skip = synchronized(contactLock) {
            if (action == 0) { penCancelled = false; false }
            else if (penCancelled && action in 1..2) { if (action == 1) penCancelled = false; true }
            else false
        }
        if (skip) return
        val msg = JSONObject().apply {
            put("type", "pen")
            put("x", x)
            put("y", y)
            put("pressure", pressure.coerceIn(0.0, 1.0))
            put("tilt_x", tiltX)
            put("tilt_y", tiltY)
            put("eraser", eraser)
            put("action", action)
        }
        synchronized(contactLock) {
            when (action) {
                0, 2 -> penContact = Pair(x, y)
                1 -> penContact = null
            }
        }
        webSocket?.send(msg.toString())
    }

    /** The S Pen's side button; the host decides what the press means (see air.py). */
    fun sendPenButton(down: Boolean) {
        Log.i(TAG, "S Pen side button ${if (down) "pressed" else "released"}")
        val msg = JSONObject().apply {
            put("type", "pen")
            put("x", 0.0)
            put("y", 0.0)
            put("pressure", 0.0)
            put("tilt_x", 0.0)
            put("tilt_y", 0.0)
            put("eraser", false)
            // 5 = stylus button down, 6 = stylus button up
            put("action", if (down) 5 else 6)
        }
        webSocket?.send(msg.toString())
    }

    /**
     * One air-motion sample from the pen's gyroscope, while its button is
     * held. Only meaningful between a button down and the matching up, which
     * is where the host recognises the gesture.
     */
    fun sendAirMotion(dx: Float, dy: Float) {
        if (!isConnected) return
        webSocket?.send(JSONObject().apply {
            put("type", "air")
            put("dx", dx.toDouble())
            put("dy", dy.toDouble())
        }.toString())
    }

    private fun sendPenProximityExit() {
        val msg = JSONObject().apply {
            put("type", "pen")
            put("x", 0.0)
            put("y", 0.0)
            put("pressure", 0.0)
            put("tilt_x", 0.0)
            put("tilt_y", 0.0)
            put("eraser", false)
            put("action", 4) // HOVER_EXIT / pen left proximity
        }
        webSocket?.send(msg.toString())
    }

    private fun sendTouch(x: Float, y: Float, pressure: Double,
                          action: Int, slot: Int) {
        val skip = synchronized(contactLock) {
            if (action == 0) { cancelledSlots.remove(slot); false }
            else if (slot in cancelledSlots) { if (action == 1) cancelledSlots.remove(slot); true }
            else false
        }
        if (skip) return
        val msg = JSONObject().apply {
            put("type", "touch")
            put("x", x.toDouble())
            put("y", y.toDouble())
            put("pressure", pressure.coerceIn(0.0, 1.0))
            put("action", action)
            put("slot", slot)
        }
        synchronized(contactLock) {
            if (action == 1) activeTouches.remove(slot) else activeTouches[slot] = Pair(x, y)
        }
        webSocket?.send(msg.toString())
    }

    // -- tablet clipboard -> computer clipboard --------------------------------
    /** Transfer id -> what to tell the user when the host has answered. */
    private val clipResults = HashMap<Int, (error: String?) -> Unit>()
    private var nextClipId = 0

    /**
     * Hand [bytes] of [mime] to the host for its clipboard, in control-channel
     * sized pieces (the host keeps its 4 KiB frame limit). [onDone] runs on
     * the socket thread with null on success or a reason to show the user.
     */
    fun sendClip(mime: String, bytes: ByteArray, onDone: (error: String?) -> Unit) {
        val ws = webSocket
        if (!isConnected || ws == null) {
            onDone("Not connected to the host")
            return
        }
        val id = synchronized(clipResults) { nextClipId += 1; clipResults[nextClipId] = onDone; nextClipId }
        scope.launch {
            var offset = 0
            var seq = 0
            do {
                val end = minOf(offset + CLIP_CHUNK, bytes.size)
                val msg = JSONObject().apply {
                    put("type", "clip")
                    put("id", id)
                    put("seq", seq)
                    put("mime", mime)
                    put("size", bytes.size)
                    put("data", Base64.encodeToString(bytes, offset, end - offset, Base64.NO_WRAP))
                    put("last", end >= bytes.size)
                }
                // OkHttp queues outgoing frames; keep the queue short so a
                // dropped socket does not take megabytes down with it.
                while (ws.queueSize() > 1_000_000) delay(5)
                if (!ws.send(msg.toString())) {
                    finishClip(id, "The connection dropped while sending")
                    return@launch
                }
                offset = end
                seq += 1
            } while (offset < bytes.size)
        }
    }

    private fun failClips(error: String) {
        val pending = synchronized(clipResults) { clipResults.values.toList().also { clipResults.clear() } }
        for (done in pending) done(error)
    }

    private fun finishClip(id: Int, error: String?) {
        val done = synchronized(clipResults) { clipResults.remove(id) } ?: return
        done(error)
    }

    /**
     * Stop (or resume) forwarding gestures. Suspending lifts every contact
     * currently down, at its last known position, so the desktop sees a
     * clean release rather than a finger held until the socket drops.
     */
    fun setInputSuspended(suspended: Boolean) {
        if (inputSuspended == suspended) return
        inputSuspended = suspended
        if (!suspended) return
        val touches: List<Pair<Int, Pair<Float, Float>>>
        val pen: Pair<Double, Double>?
        synchronized(contactLock) {
            touches = activeTouches.entries.map { Pair(it.key, it.value) }
            pen = penContact
        }
        for ((slot, at) in touches) sendTouch(at.first, at.second, 0.0, 1, slot)
        pen?.let { emitPen(it.first, it.second, 0.0, 0.0, 0.0, false, 1) }
        synchronized(contactLock) {
            // The rest of those gestures (moves, the eventual up) must not
            // reach the host: it has already seen the release.
            for ((slot, _) in touches) cancelledSlots.add(slot)
            if (pen != null) penCancelled = true
        }
        if (touches.isNotEmpty() || pen != null) {
            Log.i(TAG, "Input suspended: lifted ${touches.size} touch contact(s)" +
                if (pen != null) " and the pen" else "")
        }
    }

    /**
     * Push encoder settings to the host. The host live-restarts ffmpeg with
     * the new parameters and persists them in its config file. Settings are
     * also remembered here and re-sent on every reconnect.
     */
    fun sendConfig(bitrateKbps: Int, fps: Int) {
        val msg = JSONObject().apply {
            put("type", "config")
            put("protocol", PROTOCOL)
            put("features", JSONArray(FEATURES))
            put("bitrate", bitrateKbps)
            put("fps", fps)
        }
        pendingConfig = msg
        if (isConnected) {
            webSocket?.send(msg.toString())
            Log.i(TAG, "Sent config: $msg")
        }
    }

    /**
     * Tell the host that frame [seq] is on screen. The host started the clock
     * when it emitted that frame, so the round trip it computes is the real
     * end-to-end latency without either side needing a shared time base.
     */
    fun sendRendered(seq: Int, decodeUs: Int, renderNanos: Long) {
        if (!isConnected) return
        val msg = JSONObject().apply {
            put("type", "rendered")
            // When the frame reached the screen, on this device's monotonic
            // clock (OnFrameRendered's nanoTime): the host measures render
            // intervals from consecutive values, never mixing clock domains.
            put("render_ns", renderNanos)
            // Sent unsigned: the host's counter is a u32 and Kotlin's Int is
            // signed, so it wraps negative after ~2^31 frames (~1 year at
            // 60 fps, but free to get right).
            put("seq", seq.toLong() and 0xFFFFFFFFL)
            // How much of the round trip was spent here (arrival → on screen).
            // The host subtracts it to see what the wire actually costs.
            if (decodeUs >= 0) put("decode_us", decodeUs)
        }
        webSocket?.send(msg.toString())
    }

    /** The decoder discarded a stale backlog; an IDR now shortens the blank. */
    fun sendKeyframeRequest() {
        if (!isConnected) return
        webSocket?.send(JSONObject().put("type", "keyframe").toString())
    }

    /** Report measured tablet-side delivery, independent of the local overlay. */
    fun sendStats(decoderFps: Float, receivedMbps: Float, panelHz: Float) {
        if (!isConnected) return
        val stream = hostStreamConfig
        val msg = JSONObject().apply {
            put("type", "stats")
            put("panel_hz", panelHz.toDouble())
            put("decoder_fps", decoderFps.toDouble())
            put("received_mbps", receivedMbps.toDouble())
            put("stream_fps", stream.fps)
            put("width", stream.width)
            put("height", stream.height)
        }
        webSocket?.send(msg.toString())
    }

    /**
     * Ask the host to switch between being a second screen and being a
     * graphics tablet. The host applies it and answers with its new mode, so
     * the UI follows [onModeKnown] rather than assuming this succeeded.
     */
    fun sendMode(penOnly: Boolean) {
        val msg = JSONObject().apply {
            put("type", "mode")
            put("pen_only", penOnly)
        }
        if (isConnected) {
            webSocket?.send(msg.toString())
            Log.i(TAG, "Requested mode: ${if (penOnly) "pen-only" else "display"}")
        } else {
            // Held rather than replayed forever: the host is the source of
            // truth for the mode, and re-asserting a stale choice on every
            // reconnect would fight whatever it was set to in the meantime.
            pendingMode = msg
        }
    }

    /**
     * MotionEvent.TOOL_TYPE_PALM exists from API 29. The value is stable
     * (6) and older devices simply never report it, so comparing against the
     * number is correct everywhere; the annotation only tells lint that the
     * comparison is deliberate.
     */
    @android.annotation.SuppressLint("WrongConstant")
    private fun isPalm(event: MotionEvent, index: Int): Boolean =
        event.getToolType(index) == TOOL_TYPE_PALM

    /** A pointing device rather than a finger: a mouse, a trackball, a touchpad. */
    private fun isMouse(event: MotionEvent): Boolean {
        val source = event.source
        return source and android.view.InputDevice.SOURCE_MOUSE == android.view.InputDevice.SOURCE_MOUSE ||
            (0 until event.pointerCount).any { event.getToolType(it) == MotionEvent.TOOL_TYPE_MOUSE }
    }

    fun isControlConnected(): Boolean = isConnected

    fun disconnect() {
        wanted = false
        reconnectJob?.cancel()
        webSocket?.close(1000, "Client closing")
        webSocket = null
        isConnected = false
    }
}
