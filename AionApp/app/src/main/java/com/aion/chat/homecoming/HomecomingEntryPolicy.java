package com.aion.chat.homecoming;

public final class HomecomingEntryPolicy {
    public enum Destination {
        HOMECOMING,
        ADDRESS_PICKER,
        NORMAL_RESUME
    }

    private HomecomingEntryPolicy() {
    }

    public static Destination destination(
            boolean homecomingActive, boolean forceAddressPicker, boolean isTaskRoot) {
        if (homecomingActive) {
            return Destination.HOMECOMING;
        }
        if (forceAddressPicker) {
            return Destination.ADDRESS_PICKER;
        }
        return Destination.NORMAL_RESUME;
    }
}
