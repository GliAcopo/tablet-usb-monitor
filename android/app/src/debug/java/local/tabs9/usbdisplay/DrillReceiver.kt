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
 * Two more press the clipboard buttons of the settings sheet:
 *   adb shell am broadcast -n local.tabs9.usbdisplay/.DrillReceiver -a local.tabs9.usbdisplay.DRILL_SEND_CLIPBOARD
 *   adb shell am broadcast -n local.tabs9.usbdisplay/.DrillReceiver -a local.tabs9.usbdisplay.DRILL_SEND_SCREENSHOT
 * and one puts a known string on the tablet's clipboard first (adb cannot):
 *   adb shell am broadcast -n local.tabs9.usbdisplay/.DrillReceiver -a local.tabs9.usbdisplay.DRILL_SEED_CLIPBOARD --es text "..."
 * A synthetic S Pen press, with air motion, for testing the gesture path
 * without the pen (dx/dy per sample, in the pen's own units):
 *   adb shell am broadcast -n local.tabs9.usbdisplay/.DrillReceiver -a local.tabs9.usbdisplay.DRILL_PEN_GESTURE --ef dx 0 --ef dy -0.5 --ei steps 5
 */
class DrillReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val hook = MainActivity.drillHook
        if (hook == null) {
            Log.w(VideoReceiver.TAG, "Drill ${intent.action}: no running activity")
            return
        }
        if (intent.action == "local.tabs9.usbdisplay.DRILL_PEN_GESTURE") {
            val pen = MainActivity.penDrillHook
            if (pen == null) {
                Log.w(VideoReceiver.TAG, "Pen drill: no running activity")
                return
            }
            pen(intent.getFloatExtra("dx", 0f), intent.getFloatExtra("dy", 0f),
                intent.getIntExtra("steps", 0))
            return
        }
        if (intent.action == "local.tabs9.usbdisplay.DRILL_SEED_CLIPBOARD") {
            val text = intent.getStringExtra("text") ?: "tabs9 clipboard drill"
            context.getSystemService(android.content.ClipboardManager::class.java)
                .setPrimaryClip(android.content.ClipData.newPlainText("tabs9 drill", text))
            return
        }
        hook(intent.action ?: "")
    }
}
