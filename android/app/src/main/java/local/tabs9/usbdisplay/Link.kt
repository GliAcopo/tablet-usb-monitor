package local.tabs9.usbdisplay

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log
import java.net.Socket
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.X509TrustManager

/**
 * Where the host is: one address for the video socket and the control
 * WebSocket, plus what proves each side to the other.
 *
 * USB: 127.0.0.1 through `adb reverse`, plain TCP, the session token the host
 * put in the launch intent. Wi-Fi: the address the host announced on the LAN
 * (`_tabs9._tcp`), TLS with the host certificate pinned at pairing time, and
 * the pairing secret as the token. The host side is src/wifi.py.
 */
data class Endpoint(
    val host: String,
    val videoPort: Int,
    val controlPort: Int,
    val token: String,
    /** SHA-256 of the host certificate (hex); null means plain TCP over USB. */
    val pin: String?,
) {
    val wifi: Boolean get() = pin != null
    val label: String get() = if (wifi) "Wi-Fi $host" else "USB"
    val controlUrl: String get() = (if (wifi) "wss" else "ws") + "://$host:$controlPort"
}

/** Trusts exactly one certificate: the host's, by its SHA-256 fingerprint. */
class PinnedTrust(private val pin: String) : X509TrustManager {
    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) =
        throw CertificateException("no client certificates here")

    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        val leaf = chain?.firstOrNull() ?: throw CertificateException("no certificate")
        if (!MessageDigest.isEqual(sha256(leaf.encoded).toByteArray(), pin.toByteArray())) {
            throw CertificateException("not the paired computer's certificate")
        }
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()

    val socketFactory: SSLSocketFactory by lazy {
        SSLContext.getInstance("TLS").apply { init(null, arrayOf(this@PinnedTrust), null) }.socketFactory
    }

    /** TLS on an already connected socket; the handshake checks the pin. */
    fun wrap(plain: Socket, host: String, port: Int): SSLSocket =
        (socketFactory.createSocket(plain, host, port, true) as SSLSocket).apply { startHandshake() }

    companion object {
        fun sha256(bytes: ByteArray): String =
            MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
    }
}

/**
 * Chooses the endpoint for the next connection attempt and remembers the one
 * that worked. Only one host serves a tablet at a time (one per tablet on
 * the computer, USB or Wi-Fi), so the candidates are simply tried in turn:
 * the control channel dials [next], reports [confirmed] once the host has
 * greeted it (the token was accepted) or [failed], and the video socket
 * follows whatever the control channel confirmed ([active]).
 */
class Link(context: Context, private val prefs: Prefs) {
    companion object {
        const val TAG = "tabs9Link"
        const val SERVICE_TYPE = "_tabs9._tcp."
        const val USB_HOST = "127.0.0.1"
        const val USB_VIDEO_PORT = 8890
        const val USB_CONTROL_PORT = 8891
    }

    /** True when the USB host launched us in this process: it is certainly there. */
    @Volatile var usbLaunched = false
    @Volatile var active: Endpoint? = null
        private set
    private var failures = 0
    private val nsd = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private var discovery: NsdManager.DiscoveryListener? = null
    @Volatile private var announced: Endpoint? = null
    /** Its DNS-SD instance name: a lost *other* tablet's host must not clear it. */
    @Volatile private var announcedName: String? = null
    /** Called when the paired computer shows up on the network (or moves). */
    var onWifiHostFound: (() -> Unit)? = null

    val paired: Boolean get() = prefs.wifiSecret != null && prefs.wifiPin != null

    /** What the advert's `id` must be for our pairing (src/wifi.py tablet_id). */
    private fun expectedId(): String? = prefs.wifiSecret?.let {
        PinnedTrust.sha256("tabs9-id:$it".toByteArray()).take(16)
    }

    fun candidates(): List<Endpoint> {
        val usb = prefs.hostToken?.let { Endpoint(USB_HOST, USB_VIDEO_PORT, USB_CONTROL_PORT, it, null) }
        val wifi = ArrayList<Endpoint>()
        val secret = prefs.wifiSecret
        val pin = prefs.wifiPin
        if (secret != null && pin != null) {
            announced?.let { wifi.add(it) }
            // Fallbacks for a network that drops mDNS: the last address that
            // worked, then the computer's addresses at pairing time.
            prefs.wifiLast?.split(":")?.takeIf { it.size == 3 }?.let { (h, v, c) ->
                val vp = v.toIntOrNull(); val cp = c.toIntOrNull()
                if (vp != null && cp != null) wifi.add(Endpoint(h, vp, cp, secret, pin))
            }
            prefs.wifiHosts.forEach { wifi.add(Endpoint(it, USB_VIDEO_PORT, USB_CONTROL_PORT, secret, pin)) }
        }
        val distinctWifi = wifi.distinctBy { Triple(it.host, it.videoPort, it.controlPort) }
        // A USB start hands us a token right now; otherwise a paired tablet
        // most likely was opened by hand for Wi-Fi.
        return if (usbLaunched || !paired) listOfNotNull(usb) + distinctWifi else distinctWifi + listOfNotNull(usb)
    }

    /** The endpoint to dial now: the confirmed one, else the candidates in turn. */
    @Synchronized fun next(): Endpoint? {
        active?.let { return it }
        val all = candidates()
        if (all.isEmpty()) return null
        return all[failures % all.size]
    }

    @Synchronized fun confirmed(endpoint: Endpoint) {
        failures = 0
        if (active != endpoint) Log.i(TAG, "Connected over ${endpoint.label}")
        active = endpoint
        if (endpoint.wifi) prefs.wifiLast = "${endpoint.host}:${endpoint.videoPort}:${endpoint.controlPort}"
    }

    @Synchronized fun failed(endpoint: Endpoint) {
        if (active == endpoint) active = null else failures++
    }

    /** A new USB token or pairing: forget which endpoint worked. */
    @Synchronized fun reset() {
        active = null
        failures = 0
    }

    // -- discovery ----------------------------------------------------------------

    fun startDiscovery() {
        if (!paired || discovery != null) return
        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) { Log.i(TAG, "Looking for the computer on Wi-Fi") }
            override fun onDiscoveryStopped(serviceType: String) {}
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                Log.w(TAG, "mDNS discovery failed to start ($errorCode)")
                discovery = null
            }
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
            // Lost: the host stopped (or left the network). Forget its
            // address so the next announcement is dialled at once.
            override fun onServiceLost(info: NsdServiceInfo) {
                synchronized(this@Link) { if (info.serviceName == announcedName) announced = null }
            }
            override fun onServiceFound(info: NsdServiceInfo) { resolve(info) }
        }
        discovery = listener
        try {
            nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (e: Exception) {
            Log.w(TAG, "mDNS discovery unavailable: ${e.message}")
            discovery = null
        }
    }

    fun stopDiscovery() {
        val listener = discovery ?: return
        discovery = null
        try { nsd.stopServiceDiscovery(listener) } catch (_: Exception) {}
    }

    // NsdManager resolves one service at a time before Android 14; a second
    // request fails with FAILURE_ALREADY_ACTIVE, so they are queued.
    private val pending = ArrayDeque<NsdServiceInfo>()
    private var resolving = false

    @Synchronized private fun resolve(info: NsdServiceInfo) {
        pending.addLast(info)
        if (!resolving) resolveNext()
    }

    @Synchronized private fun resolveNext() {
        val info = pending.removeFirstOrNull() ?: run { resolving = false; return }
        resolving = true
        @Suppress("DEPRECATION")
        nsd.resolveService(info, object : NsdManager.ResolveListener {
            override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                Log.w(TAG, "Could not resolve a tabs9 host ($errorCode)")
                resolveNext()
            }

            override fun onServiceResolved(serviceInfo: NsdServiceInfo) {
                try { consider(serviceInfo) } finally { resolveNext() }
            }
        })
    }

    private fun consider(info: NsdServiceInfo) {
        val attributes = info.attributes
        val id = attributes["id"]?.toString(Charsets.US_ASCII)
        if (id == null || id != expectedId()) return   // another tablet's host, or another computer's
        @Suppress("DEPRECATION")
        val host = info.host?.hostAddress ?: return
        val control = attributes["ctl"]?.toString(Charsets.US_ASCII)?.toIntOrNull() ?: (info.port + 1)
        val endpoint = Endpoint(host, info.port, control, prefs.wifiSecret ?: return, prefs.wifiPin ?: return)
        // Announced again at the same address still means news: the host was
        // restarted, and waiting for the stale fallbacks cost half a minute.
        synchronized(this) {
            if (endpoint != announced) Log.i(TAG, "Found the paired computer on Wi-Fi at $host:${info.port}")
            announced = endpoint
            announcedName = info.serviceName
            failures = 0   // next() starts over, with the announced address first
        }
        onWifiHostFound?.invoke()
    }
}
