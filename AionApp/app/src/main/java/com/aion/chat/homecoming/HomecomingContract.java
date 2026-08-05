package com.aion.chat.homecoming;

public final class HomecomingContract {
    public static final int SCHEMA_VERSION = 1;
    public static final String[] SECTION_NAMES = {
            "identity",
            "memories",
            "timelines",
            "schedules",
            "runtime_state",
            "route_descriptors"
    };

    public static final String ERROR_INVALID_SNAPSHOT = "invalid_snapshot";
    public static final String ERROR_UNSUPPORTED_SCHEMA = "unsupported_schema";
    public static final String ERROR_STORAGE = "storage_error";

    private HomecomingContract() {
    }
}
