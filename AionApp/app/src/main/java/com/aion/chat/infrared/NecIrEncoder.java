package com.aion.chat.infrared;

public final class NecIrEncoder {
    private static final int LEADER_MARK_MICROS = 9000;
    private static final int LEADER_SPACE_MICROS = 4500;
    private static final int BIT_MARK_MICROS = 560;
    private static final int ZERO_SPACE_MICROS = 560;
    private static final int ONE_SPACE_MICROS = 1690;
    private static final int FRAME_PATTERN_LENGTH = 67;

    private NecIrEncoder() {
    }

    public static int[] encode(int address, int inverseAddress, int command) {
        int[] bytes = {
                address & 0xFF,
                inverseAddress & 0xFF,
                command & 0xFF,
                (~command) & 0xFF
        };
        int[] pattern = new int[FRAME_PATTERN_LENGTH];
        pattern[0] = LEADER_MARK_MICROS;
        pattern[1] = LEADER_SPACE_MICROS;

        int offset = 2;
        for (int value : bytes) {
            for (int bit = 0; bit < 8; bit++) {
                pattern[offset++] = BIT_MARK_MICROS;
                pattern[offset++] = ((value >> bit) & 1) == 1
                        ? ONE_SPACE_MICROS
                        : ZERO_SPACE_MICROS;
            }
        }
        pattern[offset] = BIT_MARK_MICROS;
        return pattern;
    }
}
