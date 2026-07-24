package com.aion.chat.miband;

public final class MiBandGattWritePolicy {
    private static final int ANDROID_GATT_WRITE_BUSY = 201;
    private static final int MAX_BUSY_RETRIES = 6;

    private MiBandGattWritePolicy() {}

    public static boolean shouldRetry(int status, int retriesAlreadyMade) {
        return status == ANDROID_GATT_WRITE_BUSY && retriesAlreadyMade < MAX_BUSY_RETRIES;
    }

    public static long retryDelayMillis(int retriesAlreadyMade) {
        return Math.min(250L, 80L + Math.max(0, retriesAlreadyMade) * 50L);
    }

    public static long pacingDelayMillis() {
        return 80L;
    }

    public static int writeType(int characteristicProperties) {
        final int propertyWrite = 0x08;
        final int writeTypeDefault = 0x02;
        final int writeTypeNoResponse = 0x01;
        return (characteristicProperties & propertyWrite) != 0
                ? writeTypeDefault
                : writeTypeNoResponse;
    }
}
