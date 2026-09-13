package local.tabs9.usbdisplay

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Recovery drills for debug builds:
 *   adb shell am broadcast -n local.tabs9.usbdisplay/.DrillReceiver -a local.tabs9.usbdisplay.DRILL_DROP_VIDEO   (video socket loss)
 *   adb shell am broadcast -n local.tabs9.usbdisplay/.DrillReceiver -a local.tabs9.usbdisplay.DRILL_RESET_DECODER (decoder failure)
 * Each goes through exactly the code path the corresponding real fault takes.
 */
class DrillReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val hook = MainActivity.drillHook
        if (hook == null) {
            Log.w(VideoReceiver.TAG, "Drill ${intent.action}: no running activity")
            return
        }
        hook(intent.action ?: "")
    }
}
