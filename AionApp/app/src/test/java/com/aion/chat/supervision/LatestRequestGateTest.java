package com.aion.chat.supervision;

import org.junit.Test;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class LatestRequestGateTest {
    @Test
    public void onlyNewestRequestRemainsCurrent() {
        LatestRequestGate gate = new LatestRequestGate();

        long first = gate.next();
        long second = gate.next();

        assertFalse(gate.isCurrent(first));
        assertTrue(gate.isCurrent(second));

        gate.cancel();

        assertFalse(gate.isCurrent(second));
    }
}
