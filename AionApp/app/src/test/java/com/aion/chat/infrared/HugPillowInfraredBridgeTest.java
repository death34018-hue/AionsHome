package com.aion.chat.infrared;

import org.json.JSONObject;
import org.junit.Test;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HugPillowInfraredBridgeTest {
    @Test
    public void availableEmitterReportsReadyAt38Khz() throws Exception {
        FakeTransmitter fake = new FakeTransmitter(true, true);

        JSONObject result = new JSONObject(
                new HugPillowInfraredBridge(fake).getStatus());

        assertTrue(result.getBoolean("ok"));
        assertTrue(result.getBoolean("available"));
        assertEquals(38000, result.getInt("carrierHz"));
    }

    @Test
    public void successfulCommandTransmitsExactlyOneFrameAt38Khz() throws Exception {
        FakeTransmitter fake = new FakeTransmitter(true, true);
        HugPillowInfraredBridge bridge = new HugPillowInfraredBridge(fake);

        JSONObject result = new JSONObject(bridge.transmit("POWER"));

        assertTrue(result.getBoolean("ok"));
        assertEquals(1, fake.callCount);
        assertEquals(38000, fake.carrierHz);
        assertArrayEquals(literalPowerPattern(), fake.pattern);
    }

    @Test
    public void newlyEnabledBluetoothCommandTransmitsOneFrame() throws Exception {
        FakeTransmitter fake = new FakeTransmitter(true, true);

        JSONObject result = new JSONObject(
                new HugPillowInfraredBridge(fake).transmit("BLUETOOTH"));

        assertTrue(result.getBoolean("ok"));
        assertEquals(1, fake.callCount);
        assertEquals(38000, fake.carrierHz);
        assertEquals(67, fake.pattern.length);
        assertArrayEquals(
                new int[]{
                        560, 1690, 560, 1690, 560, 1690, 560, 560,
                        560, 560, 560, 560, 560, 560, 560, 560,
                        560, 560, 560, 560, 560, 560, 560, 1690,
                        560, 1690, 560, 1690, 560, 1690, 560, 1690
                },
                java.util.Arrays.copyOfRange(fake.pattern, 34, 66));
    }

    @Test
    public void missingEmitterDoesNotTransmit() throws Exception {
        FakeTransmitter fake = new FakeTransmitter(false, true);

        JSONObject result = new JSONObject(
                new HugPillowInfraredBridge(fake).transmit("POWER"));

        assertFalse(result.getBoolean("ok"));
        assertFalse(result.getBoolean("available"));
        assertEquals("NO_EMITTER", result.getString("error"));
        assertEquals(0, fake.callCount);
    }

    @Test
    public void unsupportedCarrierDoesNotTransmit() throws Exception {
        FakeTransmitter fake = new FakeTransmitter(true, false);

        JSONObject result = new JSONObject(
                new HugPillowInfraredBridge(fake).transmit("POWER"));

        assertFalse(result.getBoolean("ok"));
        assertEquals("UNSUPPORTED_CARRIER", result.getString("error"));
        assertEquals(0, fake.callCount);
    }

    @Test
    public void unknownCommandDoesNotTransmit() throws Exception {
        FakeTransmitter fake = new FakeTransmitter(true, true);

        JSONObject result = new JSONObject(
                new HugPillowInfraredBridge(fake).transmit("NOT_A_COMMAND"));

        assertFalse(result.getBoolean("ok"));
        assertEquals("UNKNOWN_COMMAND", result.getString("error"));
        assertEquals(0, fake.callCount);
    }

    @Test
    public void systemFailureReturnsSafeMessageAndRestoresControlToCaller() throws Exception {
        FakeTransmitter fake = new FakeTransmitter(true, true);
        fake.failure = new IllegalStateException("vendor secret detail");

        JSONObject result = new JSONObject(
                new HugPillowInfraredBridge(fake).transmit("POWER"));

        assertFalse(result.getBoolean("ok"));
        assertTrue(result.getBoolean("available"));
        assertEquals("TRANSMIT_FAILED", result.getString("error"));
        assertFalse(result.toString().contains("vendor secret detail"));
        assertEquals(1, fake.callCount);
    }

    private static int[] literalPowerPattern() {
        return new int[]{
                9000, 4500,
                560, 560, 560, 560, 560, 560, 560, 560,
                560, 560, 560, 560, 560, 560, 560, 560,
                560, 1690, 560, 1690, 560, 1690, 560, 1690,
                560, 1690, 560, 1690, 560, 1690, 560, 1690,
                560, 560, 560, 1690, 560, 1690, 560, 560,
                560, 560, 560, 560, 560, 1690, 560, 560,
                560, 1690, 560, 560, 560, 560, 560, 1690,
                560, 1690, 560, 1690, 560, 560, 560, 1690,
                560
        };
    }

    static final class FakeTransmitter
            implements HugPillowInfraredBridge.IrTransmitter {
        private final boolean emitter;
        private final boolean carrierSupported;
        int callCount;
        int carrierHz;
        int[] pattern;
        RuntimeException failure;

        FakeTransmitter(boolean emitter, boolean carrierSupported) {
            this.emitter = emitter;
            this.carrierSupported = carrierSupported;
        }

        @Override
        public boolean hasEmitter() {
            return emitter;
        }

        @Override
        public boolean supportsCarrier(int carrierHz) {
            return carrierSupported;
        }

        @Override
        public void transmit(int carrierHz, int[] pattern) {
            callCount++;
            this.carrierHz = carrierHz;
            this.pattern = pattern;
            if (failure != null) {
                throw failure;
            }
        }
    }
}
