package com.aion.chat.homecoming;

import java.util.UUID;

public final class HomecomingOperationJournal {
    private HomecomingOperationJournal() {
    }

    static Operation create(
            String epochId,
            String deviceId,
            String entityType,
            String entityId,
            String action,
            String baseRevision,
            String payloadJson,
            long createdAt) {
        return new Operation(
                UUID.randomUUID().toString(),
                required(epochId, "epochId"),
                required(deviceId, "deviceId"),
                0L,
                required(entityType, "entityType"),
                required(entityId, "entityId"),
                required(action, "action"),
                baseRevision == null ? "" : baseRevision,
                required(payloadJson, "payloadJson"),
                createdAt);
    }

    public static final class Operation {
        public final String opId;
        public final String epochId;
        public final String deviceId;
        public final long deviceSeq;
        public final String entityType;
        public final String entityId;
        public final String action;
        public final String baseRevision;
        public final String payloadJson;
        public final long createdAt;

        Operation(String opId, String epochId, String deviceId, long deviceSeq,
                String entityType, String entityId, String action,
                String baseRevision, String payloadJson, long createdAt) {
            this.opId = opId;
            this.epochId = epochId;
            this.deviceId = deviceId;
            this.deviceSeq = deviceSeq;
            this.entityType = entityType;
            this.entityId = entityId;
            this.action = action;
            this.baseRevision = baseRevision;
            this.payloadJson = payloadJson;
            this.createdAt = createdAt;
        }

        Operation withDeviceSeq(long sequence) {
            return new Operation(opId, epochId, deviceId, sequence, entityType,
                    entityId, action, baseRevision, payloadJson, createdAt);
        }
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }
}
