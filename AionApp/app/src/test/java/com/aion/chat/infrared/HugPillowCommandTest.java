package com.aion.chat.infrared;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

public class HugPillowCommandTest {
    @Test
    public void exposesExactlyTheNineConfirmedCommands() {
        assertCommand("POWER", 0x46);
        assertCommand("PAT_START_STOP", 0x1C);
        assertCommand("SPEED_UP", 0x08);
        assertCommand("SPEED_DOWN", 0x5A);
        assertCommand("BLUETOOTH", 0x07);
        assertCommand("TIMER", 0x19);
        assertCommand("PREVIOUS", 0x16);
        assertCommand("NEXT", 0x0D);
        assertCommand("RECORD_PLAY", 0x43);
        assertEquals(9, HugPillowCommand.values().length);
    }

    @Test
    public void rejectsUnknownAndMissingKeys() {
        assertNull(HugPillowCommand.fromKey("NOT_A_COMMAND"));
        assertNull(HugPillowCommand.fromKey(null));
    }

    private static void assertCommand(String key, int expectedCommandByte) {
        HugPillowCommand command = HugPillowCommand.fromKey(key);
        assertEquals(0x00, command.address());
        assertEquals(0xFF, command.inverseAddress());
        assertEquals(expectedCommandByte, command.command());
    }
}
