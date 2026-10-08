package local.tabs9.usbdisplay

import org.junit.Assert.assertEquals
import org.junit.Test

class AudioJitterTest {
    @Test
    fun aStartFillsTheCushion() {
        val jitter = AudioJitter()
        assertEquals(AudioJitter.START_MS, jitter.onChunk(0, 10_000, playing = false))
        assertEquals(0, jitter.underruns)
    }

    @Test
    fun aSteadyQueueIsLeftAlone() {
        val jitter = AudioJitter()
        assertEquals(0, jitter.onChunk(55, 5, playing = true))
        assertEquals(0, jitter.onChunk(0, 0, playing = true))
        assertEquals(0, jitter.drops)
    }

    @Test
    fun aGrowingQueueDropsAChunk() {
        val jitter = AudioJitter()
        assertEquals(AudioJitter.DROP, jitter.onChunk(AudioJitter.START_MS + AudioJitter.EXCESS_MS + 5, 0, true))
        assertEquals(1, jitter.drops)
    }

    @Test
    fun aStallMidStreamGrowsTheCushion() {
        val jitter = AudioJitter()
        assertEquals(AudioJitter.START_MS + AudioJitter.STEP_MS, jitter.onChunk(0, 100, playing = true))
        assertEquals(1, jitter.underruns)
        repeat(50) { jitter.onChunk(0, 100, playing = true) }
        assertEquals(AudioJitter.MAX_MS, jitter.targetMs)
    }

    @Test
    fun aPauseOnlyRefills() {
        val jitter = AudioJitter()
        assertEquals(AudioJitter.START_MS, jitter.onChunk(0, 800, playing = true))
        assertEquals(0, jitter.underruns)
    }
}
