package local.tabs9.usbdisplay

import android.content.Context
import android.util.Log
import com.samsung.android.sdk.penremote.AirMotionEvent
import com.samsung.android.sdk.penremote.ButtonEvent
import com.samsung.android.sdk.penremote.SpenEvent
import com.samsung.android.sdk.penremote.SpenEventListener
import com.samsung.android.sdk.penremote.SpenRemote
import com.samsung.android.sdk.penremote.SpenUnit
import com.samsung.android.sdk.penremote.SpenUnitManager

/**
 * The S Pen's side button and its motion in the air, from Samsung's S Pen
 * Remote SDK.
 *
 * On this tablet the button is a Bluetooth button, not a barrel switch on
 * the digitizer: a press never reaches an app as a MotionEvent (the button
 * state of a hovering stylus is always zero) and Samsung's Air Command
 * service opens its own panel instead. The SDK is the supported way to ask
 * for it: while a foreground app is connected, the button and the air
 * gestures belong to that app.
 *
 * The SDK requires the app to be in the foreground, so [connect] and
 * [disconnect] follow the activity's resumed state.  Nothing here interprets
 * the motion: the host decides what a flick or a loop means (see air.py),
 * because that is where it can be mapped to whatever the user bound it to.
 */
class SpenButton(private val context: Context) {
    companion object {
        const val TAG = "UScreenPen"
    }

    /** The button went down (true) or up (false). */
    var onButton: ((down: Boolean) -> Unit)? = null
    /** One air-motion sample while the button is held, in the pen's own units. */
    var onAirMotion: ((dx: Float, dy: Float) -> Unit)? = null
    /** What to show in the settings sheet, in one line. */
    @Volatile var status: String = "Not connected"
        private set
    /** Called whenever [status] changes (connecting is asynchronous). */
    var onStatus: ((String) -> Unit)? = null

    private var manager: SpenUnitManager? = null
    private var wanted = false

    private fun setStatus(text: String) {
        status = text
        onStatus?.invoke(text)
    }

    // One listener per unit: a SpenEvent does not say which unit sent it, so
    // the SDK expects the listener itself to know (as its sample does).
    private val buttonListener = SpenEventListener { event: SpenEvent ->
        onButton?.invoke(ButtonEvent(event).action == ButtonEvent.ACTION_DOWN)
    }
    private val airListener = SpenEventListener { event: SpenEvent ->
        val motion = AirMotionEvent(event)
        onAirMotion?.invoke(motion.deltaX, motion.deltaY)
    }

    fun connect() {
        if (wanted) return
        wanted = true
        val remote = SpenRemote.getInstance()
        if (!remote.isFeatureEnabled(SpenRemote.FEATURE_TYPE_BUTTON)) {
            setStatus("This pen has no remote button")
            Log.i(TAG, status)
            return
        }
        setStatus("Connecting…")
        remote.connect(context, object : SpenRemote.ConnectionResultCallback {
            override fun onSuccess(unitManager: SpenUnitManager) {
                manager = unitManager
                if (!wanted) {
                    // The activity paused while the service was binding.
                    disconnect()
                    return
                }
                register(unitManager, SpenUnit.TYPE_BUTTON, buttonListener)
                setStatus(if (SpenRemote.getInstance().isFeatureEnabled(SpenRemote.FEATURE_TYPE_AIR_MOTION)) {
                    register(unitManager, SpenUnit.TYPE_AIR_MOTION, airListener)
                    "Button and air gestures"
                } else {
                    "Button only (this pen has no motion sensor)"
                })
                Log.i(TAG, "S Pen Remote connected: $status")
            }

            override fun onFailure(error: Int) {
                manager = null
                setStatus(when (error) {
                    SpenRemote.Error.UNSUPPORTED_DEVICE -> "This device has no remote S Pen"
                    SpenRemote.Error.CONNECTION_FAILED -> "The pen service refused the connection"
                    else -> "The pen service is unavailable"
                })
                Log.w(TAG, "S Pen Remote unavailable ($error): $status")
            }
        })
    }

    private fun register(unitManager: SpenUnitManager, type: Int, listener: SpenEventListener) {
        val unit = unitManager.getUnit(type) ?: return
        unitManager.registerSpenEventListener(listener, unit)
    }

    fun disconnect() {
        wanted = false
        val unitManager = manager
        manager = null
        if (unitManager != null) {
            for (type in intArrayOf(SpenUnit.TYPE_BUTTON, SpenUnit.TYPE_AIR_MOTION)) {
                val unit = unitManager.getUnit(type)
                if (unit != null) {
                    runCatching { unitManager.unregisterSpenEventListener(unit) }
                }
            }
        }
        runCatching { SpenRemote.getInstance().disconnect(context) }
        setStatus("Not connected")
    }
}
