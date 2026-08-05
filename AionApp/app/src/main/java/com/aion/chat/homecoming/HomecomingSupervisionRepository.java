package com.aion.chat.homecoming;

import android.content.ContentValues;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;

import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public final class HomecomingSupervisionRepository {
    private static final long STALE_RUNNING_MS = 5 * 60_000L;
    private static final int MAX_DIAGNOSTIC_LENGTH = 240;

    private final Backend backend;
    private final String epochId;
    private final String deviceId;

    public HomecomingSupervisionRepository(
            HomecomingDatabase database, String epochId, String deviceId) {
        this(new SQLiteBackend(database), epochId, deviceId);
    }

    HomecomingSupervisionRepository(
            Backend backend, String epochId, String deviceId) {
        if (backend == null) throw new IllegalArgumentException("backend is required");
        this.backend = backend;
        this.epochId = required(epochId, "epochId");
        this.deviceId = required(deviceId, "deviceId");
    }

    public Event enqueue(
            String eventId,
            String groupId,
            long checkpointMs,
            String roleId,
            String payloadJson,
            long now) {
        if (checkpointMs <= 0L) {
            throw new IllegalArgumentException("checkpointMs must be positive");
        }
        String checkedPayload = required(payloadJson, "payloadJson");
        try {
            new JSONObject(checkedPayload);
        } catch (Exception exception) {
            throw new IllegalArgumentException("payloadJson must be an object");
        }
        return backend.putIfAbsent(new Event(
                required(eventId, "eventId"),
                epochId,
                required(groupId, "groupId"),
                checkpointMs,
                required(roleId, "roleId"),
                checkedPayload,
                "pending",
                0,
                "",
                "",
                "",
                now,
                now));
    }

    public List<Event> recoverable(long now) {
        ArrayList<Event> currentEpoch = new ArrayList<>();
        for (Event event : backend.recoverable(now - STALE_RUNNING_MS)) {
            if (epochId.equals(event.epochId)) currentEpoch.add(event);
        }
        return Collections.unmodifiableList(currentEpoch);
    }

    public Event find(String eventId) {
        Event event = backend.find(required(eventId, "eventId"));
        return event != null && epochId.equals(event.epochId) ? event : null;
    }

    public Claim claim(String eventId, long now) {
        return backend.claim(
                required(eventId, "eventId"), now, now - STALE_RUNNING_MS);
    }

    public void complete(
            String eventId,
            String messageId,
            String resultText,
            long now) {
        String checkedEventId = required(eventId, "eventId");
        String checkedMessageId = required(messageId, "messageId");
        String checkedResult = resultText == null ? "" : resultText.trim();
        JSONObject payload = new JSONObject();
        try {
            payload.put("event_id", checkedEventId);
            payload.put("message_id", checkedMessageId);
            payload.put("result_text", checkedResult);
        } catch (Exception exception) {
            throw new IllegalStateException("operation payload failed", exception);
        }
        backend.complete(
                checkedEventId,
                checkedMessageId,
                checkedResult,
                now,
                HomecomingOperationJournal.create(
                        epochId,
                        deviceId,
                        "supervision_event",
                        checkedEventId,
                        "execute",
                        "",
                        payload.toString(),
                        now));
    }

    public void fail(String eventId, String diagnostic, long now) {
        backend.fail(
                required(eventId, "eventId"),
                sanitizeDiagnostic(diagnostic),
                now);
    }

    private static String sanitizeDiagnostic(String value) {
        String text = value == null ? "" : value
                .replace('\r', ' ')
                .replace('\n', ' ')
                .trim();
        return text.length() <= MAX_DIAGNOSTIC_LENGTH
                ? text : text.substring(0, MAX_DIAGNOSTIC_LENGTH);
    }

    interface Backend {
        Event putIfAbsent(Event event);
        Event find(String eventId);
        List<Event> recoverable(long staleBefore);
        Claim claim(String eventId, long now, long staleBefore);
        void complete(
                String eventId,
                String messageId,
                String resultText,
                long now,
                HomecomingOperationJournal.Operation operation);
        void fail(String eventId, String diagnostic, long now);
    }

    private static final class SQLiteBackend implements Backend {
        private final HomecomingDatabase helper;

        SQLiteBackend(HomecomingDatabase helper) {
            if (helper == null) throw new IllegalArgumentException("database is required");
            this.helper = helper;
        }

        @Override
        public Event putIfAbsent(Event event) {
            SQLiteDatabase database = helper.getWritableDatabase();
            database.insertWithOnConflict(
                    "supervision_event",
                    null,
                    values(event),
                    SQLiteDatabase.CONFLICT_IGNORE);
            Event stored = findRow(database, event.eventId);
            if (stored == null) throw new IllegalStateException("event insert failed");
            return stored;
        }

        @Override
        public Event find(String eventId) {
            return findRow(helper.getReadableDatabase(), eventId);
        }

        @Override
        public List<Event> recoverable(long staleBefore) {
            ArrayList<Event> events = new ArrayList<>();
            try (Cursor cursor = helper.getReadableDatabase().rawQuery(
                    "SELECT event_id,epoch_id,group_id,checkpoint_ms,role_id,"
                            + "payload_json,state,attempt_count,diagnostic,message_id,"
                            + "result_text,created_at,updated_at FROM supervision_event "
                            + "WHERE state IN ('pending','failed') "
                            + "OR (state='running' AND updated_at<?) "
                            + "ORDER BY created_at,event_id",
                    new String[]{String.valueOf(staleBefore)})) {
                while (cursor.moveToNext()) events.add(read(cursor));
            }
            return events;
        }

        @Override
        public Claim claim(String eventId, long now, long staleBefore) {
            SQLiteDatabase database = helper.getWritableDatabase();
            database.beginTransaction();
            try {
                Event current = findRow(database, eventId);
                if (current == null) {
                    database.setTransactionSuccessful();
                    return new Claim(eventId, false, "missing", 0);
                }
                boolean allowed = "pending".equals(current.state)
                        || "failed".equals(current.state)
                        || ("running".equals(current.state)
                        && current.updatedAt < staleBefore);
                if (!allowed) {
                    database.setTransactionSuccessful();
                    return new Claim(
                            eventId, false, current.state, current.attemptCount);
                }
                ContentValues update = new ContentValues();
                update.put("state", "running");
                update.put("attempt_count", current.attemptCount + 1);
                update.put("diagnostic", "");
                update.put("message_id", "");
                update.put("result_text", "");
                update.put("updated_at", now);
                database.update(
                        "supervision_event",
                        update,
                        "event_id=?",
                        new String[]{eventId});
                database.setTransactionSuccessful();
                return new Claim(
                        eventId, true, "running", current.attemptCount + 1);
            } finally {
                database.endTransaction();
            }
        }

        @Override
        public void complete(
                String eventId,
                String messageId,
                String resultText,
                long now,
                HomecomingOperationJournal.Operation operation) {
            SQLiteDatabase database = helper.getWritableDatabase();
            database.beginTransaction();
            try {
                Event current = findRow(database, eventId);
                if (current == null) throw new IllegalStateException("unknown event");
                if ("complete".equals(current.state)) {
                    database.setTransactionSuccessful();
                    return;
                }
                ContentValues update = new ContentValues();
                update.put("state", "complete");
                update.put("diagnostic", "");
                update.put("message_id", messageId);
                update.put("result_text", resultText);
                update.put("updated_at", now);
                database.update(
                        "supervision_event",
                        update,
                        "event_id=?",
                        new String[]{eventId});
                insertOperation(database, operation);
                database.setTransactionSuccessful();
            } finally {
                database.endTransaction();
            }
        }

        @Override
        public void fail(String eventId, String diagnostic, long now) {
            ContentValues update = new ContentValues();
            update.put("state", "failed");
            update.put("diagnostic", diagnostic);
            update.put("updated_at", now);
            int changed = helper.getWritableDatabase().update(
                    "supervision_event",
                    update,
                    "event_id=?",
                    new String[]{eventId});
            if (changed == 0) throw new IllegalStateException("unknown event");
        }

        private static Event findRow(SQLiteDatabase database, String eventId) {
            try (Cursor cursor = database.rawQuery(
                    "SELECT event_id,epoch_id,group_id,checkpoint_ms,role_id,"
                            + "payload_json,state,attempt_count,diagnostic,message_id,"
                            + "result_text,created_at,updated_at FROM supervision_event "
                            + "WHERE event_id=?",
                    new String[]{eventId})) {
                return cursor.moveToFirst() ? read(cursor) : null;
            }
        }

        private static Event read(Cursor cursor) {
            return new Event(
                    cursor.getString(0),
                    cursor.getString(1),
                    cursor.getString(2),
                    cursor.getLong(3),
                    cursor.getString(4),
                    cursor.getString(5),
                    cursor.getString(6),
                    cursor.getInt(7),
                    cursor.getString(8),
                    cursor.getString(9),
                    cursor.getString(10),
                    cursor.getLong(11),
                    cursor.getLong(12));
        }

        private static ContentValues values(Event event) {
            ContentValues values = new ContentValues();
            values.put("event_id", event.eventId);
            values.put("epoch_id", event.epochId);
            values.put("group_id", event.groupId);
            values.put("checkpoint_ms", event.checkpointMs);
            values.put("role_id", event.roleId);
            values.put("payload_json", event.payloadJson);
            values.put("state", event.state);
            values.put("attempt_count", event.attemptCount);
            values.put("diagnostic", event.diagnostic);
            values.put("message_id", event.messageId);
            values.put("result_text", event.resultText);
            values.put("created_at", event.createdAt);
            values.put("updated_at", event.updatedAt);
            return values;
        }

        private static void insertOperation(
                SQLiteDatabase database,
                HomecomingOperationJournal.Operation operation) {
            long sequence = 1L;
            try (Cursor cursor = database.rawQuery(
                    "SELECT COALESCE(MAX(device_seq),0)+1 FROM operation_journal",
                    null)) {
                if (cursor.moveToFirst()) sequence = cursor.getLong(0);
            }
            ContentValues values = new ContentValues();
            values.put("op_id", operation.opId);
            values.put("epoch_id", operation.epochId);
            values.put("device_id", operation.deviceId);
            values.put("device_seq", sequence);
            values.put("entity_type", operation.entityType);
            values.put("entity_id", operation.entityId);
            values.put("action", operation.action);
            values.put("base_revision", operation.baseRevision);
            values.put("payload_json", operation.payloadJson);
            values.put("created_at", operation.createdAt);
            database.insertOrThrow("operation_journal", null, values);
        }
    }

    public static final class Event {
        public final String eventId;
        public final String epochId;
        public final String groupId;
        public final long checkpointMs;
        public final String roleId;
        public final String payloadJson;
        public final String state;
        public final int attemptCount;
        public final String diagnostic;
        public final String messageId;
        public final String resultText;
        public final long createdAt;
        public final long updatedAt;

        Event(
                String eventId,
                String epochId,
                String groupId,
                long checkpointMs,
                String roleId,
                String payloadJson,
                String state,
                int attemptCount,
                String diagnostic,
                String messageId,
                String resultText,
                long createdAt,
                long updatedAt) {
            this.eventId = eventId;
            this.epochId = epochId;
            this.groupId = groupId;
            this.checkpointMs = checkpointMs;
            this.roleId = roleId;
            this.payloadJson = payloadJson;
            this.state = state;
            this.attemptCount = attemptCount;
            this.diagnostic = diagnostic;
            this.messageId = messageId;
            this.resultText = resultText;
            this.createdAt = createdAt;
            this.updatedAt = updatedAt;
        }

        Event withState(
                String nextState,
                int nextAttemptCount,
                String nextDiagnostic,
                String nextMessageId,
                String nextResultText,
                long nextUpdatedAt) {
            return new Event(
                    eventId,
                    epochId,
                    groupId,
                    checkpointMs,
                    roleId,
                    payloadJson,
                    nextState,
                    nextAttemptCount,
                    nextDiagnostic,
                    nextMessageId,
                    nextResultText,
                    createdAt,
                    nextUpdatedAt);
        }
    }

    public static final class Claim {
        public final String eventId;
        public final boolean claimed;
        public final String state;
        public final int attemptCount;

        Claim(String eventId, boolean claimed, String state, int attemptCount) {
            this.eventId = eventId;
            this.claimed = claimed;
            this.state = state;
            this.attemptCount = attemptCount;
        }
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }
}
