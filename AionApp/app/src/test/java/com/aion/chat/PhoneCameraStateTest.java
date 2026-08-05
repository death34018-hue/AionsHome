package com.aion.chat;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class PhoneCameraStateTest {
    @Test
    public void acceptsOnlyArmedFreshUniqueRequests() {
        PhoneCameraState state = new PhoneCameraState();
        long now = 10_000L;

        assertEquals(
                PhoneCameraState.Decision.DISARMED,
                state.begin("one", now + 5_000L, now)
        );

        state.arm("front", 0.8f);
        assertTrue(state.isArmed());
        assertEquals("front", state.getFacing());
        assertEquals(0.8f, state.getZoom(), 0.001f);
        assertEquals(
                PhoneCameraState.Decision.EXPIRED,
                state.begin("expired", now - 1L, now)
        );
        assertEquals(
                PhoneCameraState.Decision.ACCEPTED,
                state.begin("one", now + 5_000L, now)
        );
        assertEquals(
                PhoneCameraState.Decision.DUPLICATE,
                state.begin("one", now + 5_000L, now)
        );
        assertEquals(
                PhoneCameraState.Decision.BUSY,
                state.begin("two", now + 5_000L, now)
        );

        state.complete("one");
        assertEquals(
                PhoneCameraState.Decision.ACCEPTED,
                state.begin("two", now + 5_000L, now)
        );
    }

    @Test
    public void disarmClearsActiveRequestAndRejectsNewOnes() {
        PhoneCameraState state = new PhoneCameraState();
        state.arm("back", 2f);
        assertEquals(
                PhoneCameraState.Decision.ACCEPTED,
                state.begin("one", 20_000L, 10_000L)
        );

        state.disarm();

        assertFalse(state.isArmed());
        assertEquals(
                PhoneCameraState.Decision.DISARMED,
                state.begin("two", 20_000L, 10_000L)
        );
    }
}
