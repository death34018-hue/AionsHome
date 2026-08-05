package com.aion.chat.homecoming;

import android.content.ContentValues;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;

import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;

public final class HomecomingMemoryRepository
        implements HomecomingContextBuilder.MemoryRecall {
    private final Backend backend;
    private final String epochId;
    private final String deviceId;

    public HomecomingMemoryRepository(
            HomecomingDatabase database, String epochId, String deviceId) {
        this(new SQLiteBackend(database), epochId, deviceId);
    }

    HomecomingMemoryRepository(Backend backend, String epochId, String deviceId) {
        this.backend = backend;
        this.epochId = required(epochId, "epochId");
        this.deviceId = required(deviceId, "deviceId");
    }

    @Override
    public List<Memory> recall(String ownerId, String query, int limit) {
        validateOwner(ownerId);
        if (limit < 1 || limit > 100) {
            throw new IllegalArgumentException("limit must be 1..100");
        }
        LinkedHashMap<String, Memory> merged = new LinkedHashMap<>();
        for (Memory memory : backend.snapshots(ownerId)) {
            merged.put(memory.id, memory);
        }
        for (Memory memory : backend.locals(ownerId)) {
            if (memory.tombstone) {
                merged.remove(memory.id);
            } else {
                merged.put(memory.id, memory);
            }
        }
        final String normalizedQuery = normalize(query);
        ArrayList<Memory> ranked = new ArrayList<>(merged.values());
        ranked.sort(Comparator
                .comparingInt((Memory memory) -> score(memory, normalizedQuery))
                .reversed()
                .thenComparingLong(memory -> -memory.updatedAt)
                .thenComparing(memory -> memory.id));
        if (ranked.size() > limit) {
            ranked.subList(limit, ranked.size()).clear();
        }
        return Collections.unmodifiableList(ranked);
    }

    public Memory create(
            String ownerId, String content, String keywords, long updatedAt) {
        return save(new Memory(UUID.randomUUID().toString(), validateOwner(ownerId),
                required(content, "content"), keywords == null ? "" : keywords,
                "", false, updatedAt), "create", "");
    }

    public Memory update(String ownerId, String id, String content,
            String baseHash, long updatedAt) {
        return save(new Memory(required(id, "id"), validateOwner(ownerId),
                required(content, "content"), "", baseHash, false, updatedAt),
                "update", required(baseHash, "baseHash"));
    }

    public Memory delete(String ownerId, String id, String baseHash, long updatedAt) {
        return save(new Memory(required(id, "id"), validateOwner(ownerId),
                "", "", baseHash, true, updatedAt),
                "delete", required(baseHash, "baseHash"));
    }

    private Memory save(Memory memory, String action, String baseRevision) {
        HomecomingOperationJournal.Operation operation =
                HomecomingOperationJournal.create(
                        epochId, deviceId, "memory", memory.id, action,
                        baseRevision, payload(memory), memory.updatedAt);
        return backend.save(memory, operation);
    }

    private static String payload(Memory memory) {
        try {
            return HomecomingSnapshotStore.canonicalJson(new JSONObject()
                    .put("id", memory.id)
                    .put("owner_id", memory.ownerId)
                    .put("content", memory.content)
                    .put("keywords", memory.keywords)
                    .put("tombstone", memory.tombstone)
                    .put("updated_at", memory.updatedAt));
        } catch (Exception exception) {
            throw new IllegalStateException("could not encode memory operation", exception);
        }
    }

    private static int score(Memory memory, String query) {
        if (query.isEmpty()) {
            return 0;
        }
        int score = normalize(memory.content).contains(query) ? 20 : 0;
        for (String keyword : normalize(memory.keywords).split("[,，\\s]+")) {
            if (!keyword.isEmpty() && query.contains(keyword)) {
                score += 10 + keyword.length();
            }
        }
        for (String token : query.split("[,，。！？、\\s]+")) {
            if (!token.isEmpty() && normalize(memory.content).contains(token)) {
                score += token.length();
            }
        }
        return score;
    }

    private static String normalize(String value) {
        return value == null ? "" : value.trim().toLowerCase(Locale.ROOT);
    }

    interface Backend {
        List<Memory> snapshots(String owner);
        List<Memory> locals(String owner);
        Memory save(Memory memory, HomecomingOperationJournal.Operation operation);
    }

    private static final class SQLiteBackend implements Backend {
        private final HomecomingDatabase helper;
        SQLiteBackend(HomecomingDatabase helper) { this.helper = helper; }

        @Override public List<Memory> snapshots(String owner) {
            SQLiteDatabase database = helper.getReadableDatabase();
            ArrayList<Memory> result = new ArrayList<>();
            try (Cursor cursor = database.rawQuery(
                    "SELECT id,payload_json FROM memory_snapshot WHERE owner_id=?",
                    new String[]{owner})) {
                while (cursor.moveToNext()) {
                    try {
                        JSONObject value = new JSONObject(cursor.getString(1));
                        String rawPayload = cursor.getString(1);
                        result.add(new Memory(
                                cursor.getString(0), owner,
                                value.optString("content", ""),
                                value.optString("keywords", ""),
                                HomecomingSnapshotStore.sha256Hex(
                                        rawPayload.getBytes(
                                                java.nio.charset.StandardCharsets.UTF_8)),
                                false,
                                (long) value.optDouble("created_at", 0)));
                    } catch (Exception exception) {
                        throw new IllegalStateException("memory snapshot is invalid", exception);
                    }
                }
            }
            return result;
        }

        @Override public List<Memory> locals(String owner) {
            SQLiteDatabase database = helper.getReadableDatabase();
            ArrayList<Memory> result = new ArrayList<>();
            try (Cursor cursor = database.rawQuery(
                    "SELECT id,payload_json,base_hash,tombstone,updated_at "
                            + "FROM memory_local WHERE owner_id=?",
                    new String[]{owner})) {
                while (cursor.moveToNext()) {
                    try {
                        JSONObject value = new JSONObject(cursor.getString(1));
                        result.add(new Memory(cursor.getString(0), owner,
                                value.optString("content", ""),
                                value.optString("keywords", ""),
                                cursor.getString(2), cursor.getInt(3) != 0,
                                cursor.getLong(4)));
                    } catch (Exception exception) {
                        throw new IllegalStateException("local memory is invalid", exception);
                    }
                }
            }
            return result;
        }

        @Override
        public Memory save(
                Memory memory, HomecomingOperationJournal.Operation operation) {
            SQLiteDatabase database = helper.getWritableDatabase();
            database.beginTransaction();
            try {
                ContentValues row = new ContentValues();
                row.put("id", memory.id);
                row.put("owner_id", memory.ownerId);
                row.put("payload_json", payload(memory));
                row.put("base_hash", memory.baseHash);
                row.put("tombstone", memory.tombstone ? 1 : 0);
                row.put("updated_at", memory.updatedAt);
                database.insertWithOnConflict(
                        "memory_local", null, row, SQLiteDatabase.CONFLICT_REPLACE);
                insertOperation(database, operation);
                database.setTransactionSuccessful();
                return memory;
            } finally {
                database.endTransaction();
            }
        }

        private static void insertOperation(
                SQLiteDatabase database, HomecomingOperationJournal.Operation operation) {
            long sequence = 1L;
            try (Cursor cursor = database.rawQuery(
                    "SELECT COALESCE(MAX(device_seq),0)+1 FROM operation_journal", null)) {
                if (cursor.moveToFirst()) sequence = cursor.getLong(0);
            }
            ContentValues row = new ContentValues();
            row.put("op_id", operation.opId);
            row.put("epoch_id", operation.epochId);
            row.put("device_id", operation.deviceId);
            row.put("device_seq", sequence);
            row.put("entity_type", operation.entityType);
            row.put("entity_id", operation.entityId);
            row.put("action", operation.action);
            row.put("base_revision", operation.baseRevision);
            row.put("payload_json", operation.payloadJson);
            row.put("created_at", operation.createdAt);
            database.insertOrThrow("operation_journal", null, row);
        }
    }

    public static final class Memory {
        public final String id;
        public final String ownerId;
        public final String content;
        public final String keywords;
        public final String baseHash;
        public final boolean tombstone;
        public final long updatedAt;

        public Memory(String id, String ownerId, String content, String keywords,
                String baseHash, boolean tombstone, long updatedAt) {
            this.id = id;
            this.ownerId = ownerId;
            this.content = content;
            this.keywords = keywords;
            this.baseHash = baseHash;
            this.tombstone = tombstone;
            this.updatedAt = updatedAt;
        }
    }

    private static String validateOwner(String owner) {
        if (!"main".equals(owner) && !"second".equals(owner)) {
            throw new IllegalArgumentException("unsupported memory owner");
        }
        return owner;
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }
}
