package com.aion.chat.infrared;

import org.junit.Test;

import static org.junit.Assert.assertEquals;

public class NecIrEncoderTest {
    @Test
    public void powerCodeProducesOneStandardNecFrameInLsbFirstOrder() {
        int[] pattern = NecIrEncoder.encode(0x00, 0xFF, 0x46);

        assertEquals(67, pattern.length);
        assertEquals(9000, pattern[0]);
        assertEquals(4500, pattern[1]);
        assertByte(pattern, 0, 0x00);
        assertByte(pattern, 1, 0xFF);
        assertByte(pattern, 2, 0x46);
        assertByte(pattern, 3, 0xB9);
        assertEquals(560, pattern[66]);
    }

    @Test
    public void speedUpCodeUsesAnEightBitCommandComplement() {
        int[] pattern = NecIrEncoder.encode(0x00, 0xFF, 0x08);

        assertByte(pattern, 2, 0x08);
        assertByte(pattern, 3, 0xF7);
    }

    private static void assertByte(int[] pattern, int byteIndex, int expected) {
        int[] expectedSpaces = new int[8];
        for (int bit = 0; bit < 8; bit++) {
            expectedSpaces[bit] = ((expected >> bit) & 1) == 1 ? 1690 : 560;
        }
        for (int bit = 0; bit < 8; bit++) {
            int offset = 2 + byteIndex * 16 + bit * 2;
            assertEquals("mark at bit " + bit, 560, pattern[offset]);
            assertEquals("space at bit " + bit, expectedSpaces[bit], pattern[offset + 1]);
        }
    }
}
