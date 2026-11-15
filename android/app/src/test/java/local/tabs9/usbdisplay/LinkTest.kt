package local.tabs9.usbdisplay

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LinkTest {
    @Test fun hostsWorthDialling() {
        assertTrue(Link.validHost("192.168.1.81"))
        assertTrue(Link.validHost("100.104.68.62"))
        assertTrue(Link.validHost("jacopo-main.tail1234.ts.net"))
        assertFalse(Link.validHost("127.0.0.1"))
        assertFalse(Link.validHost("0.0.0.0"))
        assertFalse(Link.validHost("255.255.255.255"))
        assertFalse(Link.validHost("300.1.1.1"))
        assertFalse(Link.validHost("1.2.3"))
        assertFalse(Link.validHost("localhost"))
        assertFalse(Link.validHost("evil host"))
        assertFalse(Link.validHost(""))
    }

    @Test fun tailnetAddresses() {
        assertTrue(Link.isTailnet("100.64.0.1"))
        assertTrue(Link.isTailnet("100.127.255.254"))
        assertTrue(Link.isTailnet("pc.tail1234.ts.net"))
        assertFalse(Link.isTailnet("100.128.0.1"))
        assertFalse(Link.isTailnet("192.168.1.81"))
    }

    @Test fun theHostsKnockLine() {
        // What src/wifi.py knock() sends (json.dumps, default separators).
        val line = """{"tabs9": 1, "id": "0123456789abcdef", "video": 8893, "control": 8894}"""
        assertEquals(Link.Companion.Knock("0123456789abcdef", 8893, 8894), Link.parseKnock(line))
        assertNull(Link.parseKnock("""{"tabs9": 2, "id": "0123456789abcdef", "video": 8893, "control": 8894}"""))
        assertNull(Link.parseKnock("""{"tabs9": 1, "id": "short", "video": 8893, "control": 8894}"""))
        assertNull(Link.parseKnock("""{"tabs9": 1, "id": "0123456789abcdef", "video": 80, "control": 8894}"""))
        assertNull(Link.parseKnock("""{"tabs9": 1, "id": "0123456789abcdef", "video": 8893}"""))
        assertNull(Link.parseKnock("x".repeat(600)))
    }

    @Test fun theSameNetworkIsDialledBeforeTailscale() {
        fun e(host: String) = Endpoint(host, 8890, 8891, "s", "p")
        val order = Link.raceOrder(listOf(e("100.104.68.62"), e("192.168.1.81"), e("pc.x.ts.net"), e("10.0.0.2")))
        assertEquals(listOf("192.168.1.81", "10.0.0.2", "100.104.68.62", "pc.x.ts.net"), order.map { it.host })
    }
}
