package local.tabs9.usbdisplay

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.atomic.AtomicInteger
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
 *
 * On Wi-Fi the host may be at several addresses (the LAN, Tailscale) and
 * any of them may be unreachable from here, so they are all dialled at once
 * ([Link.race]) and the first that answers is used.
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
        /** Where the host knocks while this app is open and paired (src/wifi.py KNOCK_PORT). */
        const val KNOCK_PORT = 8889
        /** How long one address gets to accept a TCP connection in a race. */
        const val RACE_CONNECT_MS = 3000
        /** Head start of each address over the next one in a race, in list order. */
        const val RACE_STAGGER_MS = 120L
        private const val MAX_HOSTS = 8

        private val IPV4 = Regex("""\d{1,3}(\.\d{1,3}){3}""")
        private val DNS_NAME = Regex("""[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?""")

        /** An address or name worth dialling: IPv4 or a dotted DNS name (Tailscale's MagicDNS). */
        fun validHost(host: String): Boolean {
            if (IPV4.matches(host)) return host.split(".").all { it.toInt() in 0..255 } &&
                !host.startsWith("127.") && !host.startsWith("0.") && host != "255.255.255.255"
            return DNS_NAME.matches(host) && host.contains('.') && !host.all { it.isDigit() || it == '.' }
        }

        /** Tailscale's address range, 100.64.0.0/10. */
        fun isTailnet(host: String): Boolean {
            if (!IPV4.matches(host)) return host.endsWith(".ts.net")
            val (a, b) = host.split(".").map { it.toInt() }
            return a == 100 && b in 64..127
        }

        data class Knock(val id: String, val video: Int, val control: Int)

        /** The host's knock line: {"tabs9": 1, "id": "...", "video": 8890, "control": 8891}. */
        fun parseKnock(line: String): Knock? {
            if (line.length > 512 || !Regex(""""tabs9"\s*:\s*1\b""").containsMatchIn(line)) return null
            val id = Regex(""""id"\s*:\s*"([0-9a-f]{16})"""").find(line)?.groupValues?.get(1) ?: return null
            fun port(name: String) = Regex(""""$name"\s*:\s*(\d{1,5})\b""").find(line)
                ?.groupValues?.get(1)?.toInt()?.takeIf { it in 1024..65535 }
            return Knock(id, port("video") ?: return null, port("control") ?: return null)
        }

        /** Same network first (a few milliseconds away), then Tailscale; otherwise as listed. */
        fun raceOrder(endpoints: List<Endpoint>): List<Endpoint> =
            endpoints.sortedBy { if (isTailnet(it.host)) 1 else 0 }

        /** This tablet's own IPv4 addresses, told to the host so it can knock. */
        fun ownAddresses(): List<String> = try {
            NetworkInterface.getNetworkInterfaces().toList()
                .filter { it.isUp && !it.isLoopback }
                .flatMap { it.inetAddresses.toList() }
                .filterIsInstance<Inet4Address>()
                .filter { !it.isLinkLocalAddress && !it.isLoopbackAddress }
                .mapNotNull { it.hostAddress }
                .distinct()
        } catch (_: Exception) { emptyList() }
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
    /** The last knock from the paired host: it is up at the sender's address. */
    @Volatile private var knocked: Endpoint? = null
    private var lastKnockNotice = 0L
    private var knockServer: ServerSocket? = null
    private val io = CoroutineScope(Dispatchers.IO + SupervisorJob())
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
            knocked?.let { wifi.add(it) }
            // Fallbacks for a network that drops mDNS: the last address that
            // worked, then every address the computer has told us about (at
            // pairing, and in each greeting since), on the ports it last used.
            var ports = USB_VIDEO_PORT to USB_CONTROL_PORT
            (announced ?: knocked)?.let { ports = it.videoPort to it.controlPort }
            prefs.wifiLast?.split(":")?.takeIf { it.size == 3 }?.let { (h, v, c) ->
                val vp = v.toIntOrNull(); val cp = c.toIntOrNull()
                if (vp != null && cp != null) {
                    wifi.add(Endpoint(h, vp, cp, secret, pin))
                    if (announced == null && knocked == null) ports = vp to cp
                }
            }
            prefs.wifiHosts.forEach { wifi.add(Endpoint(it, ports.first, ports.second, secret, pin)) }
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

    /** The Wi-Fi candidates, for [race]. */
    fun wifiCandidates(): List<Endpoint> = candidates().filter { it.wifi }

    /**
     * Dial every Wi-Fi candidate at once (plain TCP to the control port, the
     * nearer ones a little earlier) and return the first that accepts, or
     * null when none does within [RACE_CONNECT_MS]. Trying them one after
     * the other cost a connect timeout per dead address, and a dead address
     * is the usual case: an old DHCP lease, a LAN this tablet is not on, a
     * firewall. The losers are closed as they finish.
     */
    suspend fun race(): Endpoint? {
        val endpoints = raceOrder(wifiCandidates())
        if (endpoints.isEmpty()) return null
        if (endpoints.size == 1) return endpoints[0]
        val winner = CompletableDeferred<Endpoint?>()
        val left = AtomicInteger(endpoints.size)
        val jobs = endpoints.mapIndexed { i, endpoint ->
            io.launch {
                delay(i * RACE_STAGGER_MS)
                if (winner.isCompleted) return@launch
                val ok = try {
                    Socket().use { it.connect(InetSocketAddress(endpoint.host, endpoint.controlPort), RACE_CONNECT_MS); true }
                } catch (_: Exception) { false }
                if (ok && winner.complete(endpoint)) Log.i(TAG, "Reached the computer at ${endpoint.host}")
                if (left.decrementAndGet() == 0) winner.complete(null)
            }
        }
        val result = withTimeoutOrNull(RACE_CONNECT_MS + endpoints.size * RACE_STAGGER_MS + 500) { winner.await() }
        jobs.forEach { it.cancel() }
        return result
    }

    /** The host's current addresses, from its greeting: kept for the next connection. */
    fun learnHosts(hosts: List<String>) {
        val fresh = hosts.map { it.trim() }.filter { validHost(it) }
        if (fresh.isEmpty()) return
        val merged = (fresh + prefs.wifiHosts.filter { it !in fresh }).take(MAX_HOSTS)
        if (merged != prefs.wifiHosts) prefs.wifiHosts = merged
    }

    /** A new USB token or pairing: forget which endpoint worked. */
    @Synchronized fun reset() {
        active = null
        failures = 0
    }

    // -- knock (src/wifi.py knock()) ---------------------------------------------

    /**
     * Listen for the host's knock while the app is in front and paired. The
     * host connects to us, which works where we cannot find or reach it
     * first: no mDNS on the network, or a computer that never hears our ARP
     * requests. The knock is only a hint: its sender is dialled like any
     * other candidate, and the pinned certificate still decides.
     */
    @Synchronized fun startKnockListener() {
        if (!paired || knockServer != null) return
        val server = try {
            ServerSocket().apply { reuseAddress = true; bind(InetSocketAddress(KNOCK_PORT)) }
        } catch (e: Exception) {
            Log.w(TAG, "Cannot listen for the computer's knock on $KNOCK_PORT: ${e.message}")
            return
        }
        knockServer = server
        Thread({
            while (!server.isClosed) {
                val socket = try { server.accept() } catch (_: Exception) { break }
                try { socket.use { heard(it) } } catch (_: Exception) {}
            }
        }, "tabs9-knock").apply { isDaemon = true; start() }
    }

    @Synchronized fun stopKnockListener() {
        try { knockServer?.close() } catch (_: Exception) {}
        knockServer = null
    }

    private fun heard(socket: Socket) {
        socket.soTimeout = 1500
        val input = socket.getInputStream()
        val bytes = java.io.ByteArrayOutputStream()
        while (bytes.size() < 512) {
            val b = input.read()
            if (b < 0 || b == '\n'.code) break
            bytes.write(b)
        }
        val knock = parseKnock(bytes.toString(Charsets.US_ASCII.name())) ?: return
        if (knock.id != expectedId()) return   // another tablet's host
        val host = (socket.inetAddress as? Inet4Address)?.hostAddress ?: return
        val endpoint = Endpoint(host, knock.video, knock.control, prefs.wifiSecret ?: return, prefs.wifiPin ?: return)
        val now = android.os.SystemClock.elapsedRealtime()
        val news = synchronized(this) {
            val changed = endpoint != knocked
            if (changed) Log.i(TAG, "The paired computer knocked from $host")
            knocked = endpoint
            failures = 0
            // The host knocks every few seconds: a redial on each would cut
            // short the race the previous one started.
            (changed || now - lastKnockNotice >= 6000).also { if (it) lastKnockNotice = now }
        }
        if (news && active == null) onWifiHostFound?.invoke()
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
