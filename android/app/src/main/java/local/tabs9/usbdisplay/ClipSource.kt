package local.tabs9.usbdisplay

import android.content.ClipboardManager
import android.content.ContentUris
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.provider.MediaStore
import java.io.ByteArrayOutputStream

/**
 * What the tablet can hand to the computer's clipboard: its own clipboard
 * (text, or an image copied from the gallery, a browser, the screenshot
 * toolbar...) or the newest screenshot on it.
 *
 * Reading the clipboard only works while this app is the focused one
 * (Android 10+), which it is when the settings sheet is open. Reading
 * screenshots needs the images permission; the activity asks for it.
 */
object ClipSource {
    /** The most the host accepts; a 2960x1848 PNG screenshot is 2-5 MB. */
    const val MAX_BYTES = 32 * 1024 * 1024

    class Clip(val mime: String, val bytes: ByteArray, val what: String)

    fun fromClipboard(context: Context): Result<Clip> {
        val manager = context.getSystemService(ClipboardManager::class.java)
        val clip = manager?.primaryClip
        if (clip == null || clip.itemCount == 0) return Result.failure(Exception("The tablet's clipboard is empty"))
        val item = clip.getItemAt(0)
        item.uri?.let { uri ->
            val fromUri = fromUri(context, uri, "the copied image")
            if (fromUri.isSuccess) return fromUri
        }
        val text = item.coerceToText(context)?.toString() ?: ""
        if (text.isEmpty()) return Result.failure(Exception("The tablet's clipboard holds nothing this can send"))
        return Result.success(Clip("text/plain", text.toByteArray(Charsets.UTF_8), "${text.length} characters"))
    }

    fun latestScreenshot(context: Context): Result<Clip> {
        val projection = arrayOf(MediaStore.Images.Media._ID, MediaStore.Images.Media.DISPLAY_NAME)
        val cursor = context.contentResolver.query(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI, projection,
            "${MediaStore.Images.Media.RELATIVE_PATH} LIKE ?", arrayOf("%Screenshots%"),
            "${MediaStore.Images.Media.DATE_ADDED} DESC"
        ) ?: return Result.failure(Exception("Could not look for screenshots"))
        cursor.use {
            if (!it.moveToFirst()) return Result.failure(Exception("No screenshot found on the tablet"))
            val id = it.getLong(0)
            val name = it.getString(1) ?: "screenshot"
            val uri = ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id)
            return fromUri(context, uri, name)
        }
    }

    /**
     * PNG and JPEG go as they are, anything else the tablet can decode is
     * re-encoded as PNG so the desktop is sure to understand it.
     */
    private fun fromUri(context: Context, uri: Uri, what: String): Result<Clip> {
        val type = context.contentResolver.getType(uri) ?: ""
        if (!type.startsWith("image/")) return Result.failure(Exception("The copied item is not an image ($type)"))
        val bytes = try {
            context.contentResolver.openInputStream(uri)?.use { it.readBytes() }
        } catch (e: Exception) {
            return Result.failure(Exception("Could not read $what: ${e.message}"))
        } ?: return Result.failure(Exception("Could not open $what"))
        if (bytes.size > MAX_BYTES) return Result.failure(Exception("$what is larger than ${MAX_BYTES / (1024 * 1024)} MB"))
        if (type == "image/png" || type == "image/jpeg") return Result.success(Clip(type, bytes, what))
        val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            ?: return Result.failure(Exception("Could not decode $what ($type)"))
        val out = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.PNG, 100, out)
        bitmap.recycle()
        if (out.size() > MAX_BYTES) return Result.failure(Exception("$what is too large once converted"))
        return Result.success(Clip("image/png", out.toByteArray(), what))
    }
}
