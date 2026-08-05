package com.aion.chat.infrared;

public enum HugPillowCommand {
    POWER("POWER", 0x00, 0xFF, 0x46),
    PAT_START_STOP("PAT_START_STOP", 0x00, 0xFF, 0x1C),
    SPEED_UP("SPEED_UP", 0x00, 0xFF, 0x08),
    SPEED_DOWN("SPEED_DOWN", 0x00, 0xFF, 0x5A),
    BLUETOOTH("BLUETOOTH", 0x00, 0xFF, 0x07),
    TIMER("TIMER", 0x00, 0xFF, 0x19),
    PREVIOUS("PREVIOUS", 0x00, 0xFF, 0x16),
    NEXT("NEXT", 0x00, 0xFF, 0x0D),
    RECORD_PLAY("RECORD_PLAY", 0x00, 0xFF, 0x43);

    private final String key;
    private final int address;
    private final int inverseAddress;
    private final int command;

    HugPillowCommand(String key, int address, int inverseAddress, int command) {
        this.key = key;
        this.address = address;
        this.inverseAddress = inverseAddress;
        this.command = command;
    }

    public int address() {
        return address;
    }

    public int inverseAddress() {
        return inverseAddress;
    }

    public int command() {
        return command;
    }

    public static HugPillowCommand fromKey(String key) {
        if (key == null) {
            return null;
        }
        for (HugPillowCommand command : values()) {
            if (command.key.equals(key)) {
                return command;
            }
        }
        return null;
    }
}
