package com.aion.chat.homecoming;

import android.content.ContentValues;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;

import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.UUID;

public final class HomecomingChatRepository {
    private final Backend backend;
    private final String epochId;
    private final String deviceId;

    public HomecomingChatRepository(
            HomecomingDatabase database, String epochId, String deviceId) {
        this(new SQLiteBackend(database), epochId, deviceId);
    }

    HomecomingChatRepository(Backend backend, String epochId, String deviceId) {
        this.backend = backend;
        this.epochId = required(epochId, "epochId");
        this.deviceId = required(deviceId, "deviceId");
    }

    public Message commitUserMessage(
            String requestId, String timelineId, String senderId, String text,
            String attachmentKind, long createdAt) {
        return commit(requestId, timelineId, "user", senderId, text,
                attachmentKind, "", createdAt, false);
    }

    public Message commitAssistantMessage(
            String requestId, String timelineId, String senderId,
            String completeText, long createdAt) {
        return commit(requestId, timelineId, "assistant", senderId,
                completeText, "", "", createdAt, true);
    }

    public Message commitSystemMessage(
            String resultRequestId,
            String timelineId,
            String text,
            long createdAt) {
        return commit(
                resultRequestId,
                timelineId,
                "system",
                "system",
                text,
                "",
                "",
                createdAt,
                false);
    }

    public void failRequest(String requestId, String failureCode) {
        backend.fail(required(requestId, "requestId"),
                required(failureCode, "failureCode"));
    }

    public void beginAssistantRequest(
            String requestId, String timelineId, long createdAt) {
        validateTimeline(timelineId);
        backend.begin(
                required(requestId, "requestId"), timelineId, createdAt);
    }

    public List<Message> listMessages(
            String timelineId, long beforeCreatedAt, int limit) {
        validateTimeline(timelineId);
        if (limit < 1 || limit > 500) {
            throw new IllegalArgumentException("limit must be 1..500");
        }
        return backend.list(epochId, timelineId, beforeCreatedAt, limit);
    }

    public void recordDeferredControls(
            String requestId,
            List<HomecomingControlParser.ControlEvent> controls,
            long createdAt) {
        for (int index = 0; index < controls.size(); index++) {
            HomecomingControlParser.ControlEvent control = controls.get(index);
            String entityId = required(requestId, "requestId")
                    + ":" + index + ":" + control.type;
            HomecomingOperationJournal.Operation operation =
                    HomecomingOperationJournal.create(
                            epochId, deviceId, "deferred_control", entityId,
                            "create", "", control.rawTag, createdAt);
            backend.recordControl(entityId, operation);
        }
    }

    private Message commit(
            String requestId, String timelineId, String role, String senderId,
            String text, String attachmentKind, String transcript,
            long createdAt, boolean assistant) {
        validateTimeline(timelineId);
        String normalizedText = text == null ? "" : text.trim();
        if (normalizedText.isEmpty() && (attachmentKind == null || attachmentKind.isEmpty())) {
            throw new IllegalArgumentException("text or attachment is required");
        }
        Message candidate = new Message(
                UUID.randomUUID().toString(),
                required(requestId, "requestId"),
                epochId,
                timelineId,
                role,
                required(senderId, "senderId"),
                normalizedText,
                attachmentKind == null ? "" : attachmentKind,
                transcript == null ? "" : transcript,
                createdAt,
                "committed");
        HomecomingOperationJournal.Operation operation =
                HomecomingOperationJournal.create(
                        epochId,
                        deviceId,
                        "message",
                        candidate.id,
                        "create",
                        "",
                        payload(candidate),
                        createdAt);
        return backend.commit(candidate, operation, assistant);
    }

    private static String payload(Message message) {
        try {
            return HomecomingSnapshotStore.canonicalJson(new JSONObject()
                    .put("id", message.id)
                    .put("request_id", message.requestId)
                    .put("epoch_id", message.epochId)
                    .put("timeline_id", message.timelineId)
                    .put("role", message.role)
                    .put("sender_id", message.senderId)
                    .put("text", message.text)
                    .put("attachment_kind", message.attachmentKind)
                    .put("attachment_transcript", message.attachmentTranscript)
                    .put("created_at", message.createdAt));
        } catch (Exception exception) {
            throw new IllegalStateException("could not encode message operation", exception);
        }
    }

    private static void validateTimeline(String timelineId) {
        if (!"main_private".equals(timelineId)
                && !"companion_private".equals(timelineId)
                && !"group".equals(timelineId)) {
            throw new IllegalArgumentException("unsupported timeline");
        }
    }

    interface Backend {
        Message commit(
                Message candidate,
                HomecomingOperationJournal.Operation operation,
                boolean assistant);
        void fail(String requestId, String code);
        default void begin(String requestId, String timelineId, long createdAt) {
        }
        List<Message> list(
                String epochId, String timelineId, long beforeCreatedAt, int limit);
        default void recordControl(
                String entityId, HomecomingOperationJournal.Operation operation) {
        }
    }

    private static final class SQLiteBackend implements Backend {
        private final HomecomingDatabase helper;

        SQLiteBackend(HomecomingDatabase helper) {
            this.helper = helper;
        }

        @Override
        public Message commit(
                Message candidate,
                HomecomingOperationJournal.Operation operation,
                boolean assistant) {
            SQLiteDatabase database = helper.getWritableDatabase();
            database.beginTransaction();
            try {
                Message existing = find(database, candidate.requestId, candidate.role);
                if (existing != null) {
                    database.setTransactionSuccessful();
                    return existing;
                }
                String state = requestState(database, candidate.requestId);
                if (assistant && "failed".equals(state)) {
                    throw new IllegalStateException("request already failed");
                }
                upsertRequest(database, candidate, assistant ? "complete" : "pending");
                insertMessage(database, candidate);
                insertOperation(database, operation);
                database.setTransactionSuccessful();
                return candidate;
            } finally {
                database.endTransaction();
            }
        }

        @Override
        public void fail(String requestId, String code) {
            SQLiteDatabase database = helper.getWritableDatabase();
            ContentValues values = new ContentValues();
            values.put("state", "failed");
            values.put("failure_code", code);
            values.put("updated_at", System.currentTimeMillis());
            int changed = database.update(
                    "chat_request", values, "request_id=?", new String[]{requestId});
            if (changed == 0) {
                throw new IllegalStateException("unknown request");
            }
        }

        @Override
        public void begin(String requestId, String timelineId, long createdAt) {
            SQLiteDatabase database = helper.getWritableDatabase();
            ContentValues values = new ContentValues();
            values.put("request_id", requestId);
            values.put("timeline_id", timelineId);
            values.put("state", "pending");
            values.put("created_at", createdAt);
            values.put("updated_at", createdAt);
            database.insertWithOnConflict(
                    "chat_request", null, values, SQLiteDatabase.CONFLICT_IGNORE);
        }

        @Override
        public List<Message> list(
                String epochId, String timelineId, long beforeCreatedAt, int limit) {
            SQLiteDatabase database = helper.getReadableDatabase();
            ArrayList<Message> rows = new ArrayList<>();
            try (Cursor cursor = database.rawQuery(
                    "SELECT id,request_id,epoch_id,timeline_id,role,sender_id,text_content,"
                            + "attachment_kind,attachment_transcript,created_at,delivery_state "
                            + "FROM chat_message "
                            + "WHERE epoch_id=? AND timeline_id=? AND created_at<? "
                            + "ORDER BY created_at DESC,id DESC LIMIT ?",
                    new String[]{epochId, timelineId, String.valueOf(beforeCreatedAt),
                            String.valueOf(limit)})) {
                while (cursor.moveToNext()) {
                    rows.add(readMessage(cursor));
                }
            }
            Collections.reverse(rows);
            return Collections.unmodifiableList(rows);
        }

        @Override
        public void recordControl(
                String entityId, HomecomingOperationJournal.Operation operation) {
            SQLiteDatabase database = helper.getWritableDatabase();
            try (Cursor cursor = database.rawQuery(
                    "SELECT 1 FROM operation_journal "
                            + "WHERE entity_type='deferred_control' AND entity_id=? LIMIT 1",
                    new String[]{entityId})) {
                if (cursor.moveToFirst()) {
                    return;
                }
            }
            database.beginTransaction();
            try {
                insertOperation(database, operation);
                database.setTransactionSuccessful();
            } finally {
                database.endTransaction();
            }
        }

        private static void upsertRequest(
                SQLiteDatabase database, Message message, String state) {
            ContentValues values = new ContentValues();
            values.put("request_id", message.requestId);
            values.put("timeline_id", message.timelineId);
            values.put("state", state);
            values.put("created_at", message.createdAt);
            values.put("updated_at", message.createdAt);
            database.insertWithOnConflict(
                    "chat_request", null, values, SQLiteDatabase.CONFLICT_IGNORE);
            ContentValues update = new ContentValues();
            update.put("state", state);
            update.put("updated_at", message.createdAt);
            database.update("chat_request", update, "request_id=?",
                    new String[]{message.requestId});
        }

        private static void insertMessage(SQLiteDatabase database, Message message) {
            ContentValues values = new ContentValues();
            values.put("id", message.id);
            values.put("request_id", message.requestId);
            values.put("epoch_id", message.epochId);
            values.put("timeline_id", message.timelineId);
            values.put("role", message.role);
            values.put("sender_id", message.senderId);
            values.put("text_content", message.text);
            values.put("attachment_kind", message.attachmentKind);
            values.put("attachment_transcript", message.attachmentTranscript);
            values.put("created_at", message.createdAt);
            values.put("delivery_state", message.deliveryState);
            database.insertOrThrow("chat_message", null, values);
        }

        private static void insertOperation(
                SQLiteDatabase database, HomecomingOperationJournal.Operation operation) {
            long sequence = 1L;
            try (Cursor cursor = database.rawQuery(
                    "SELECT COALESCE(MAX(device_seq),0)+1 FROM operation_journal", null)) {
                if (cursor.moveToFirst()) {
                    sequence = cursor.getLong(0);
                }
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

        private static Message find(SQLiteDatabase database, String requestId, String role) {
            try (Cursor cursor = database.rawQuery(
                    "SELECT id,request_id,epoch_id,timeline_id,role,sender_id,text_content,"
                            + "attachment_kind,attachment_transcript,created_at,delivery_state "
                            + "FROM chat_message WHERE request_id=? AND role=?",
                    new String[]{requestId, role})) {
                return cursor.moveToFirst() ? readMessage(cursor) : null;
            }
        }

        private static String requestState(SQLiteDatabase database, String requestId) {
            try (Cursor cursor = database.rawQuery(
                    "SELECT state FROM chat_request WHERE request_id=?",
                    new String[]{requestId})) {
                return cursor.moveToFirst() ? cursor.getString(0) : "";
            }
        }

        private static Message readMessage(Cursor cursor) {
            return new Message(
                    cursor.getString(0), cursor.getString(1), cursor.getString(2),
                    cursor.getString(3), cursor.getString(4), cursor.getString(5),
                    cursor.getString(6), cursor.getString(7), cursor.getString(8),
                    cursor.getLong(9), cursor.getString(10));
        }
    }

    public static final class Message {
        public final String id;
        public final String requestId;
        public final String epochId;
        public final String timelineId;
        public final String role;
        public final String senderId;
        public final String text;
        public final String attachmentKind;
        public final String attachmentTranscript;
        public final long createdAt;
        public final String deliveryState;

        Message(String id, String requestId, String epochId, String timelineId, String role,
                String senderId, String text, String attachmentKind,
                String attachmentTranscript, long createdAt, String deliveryState) {
            this.id = id;
            this.requestId = requestId;
            this.epochId = epochId;
            this.timelineId = timelineId;
            this.role = role;
            this.senderId = senderId;
            this.text = text;
            this.attachmentKind = attachmentKind;
            this.attachmentTranscript = attachmentTranscript;
            this.createdAt = createdAt;
            this.deliveryState = deliveryState;
        }

        Message(String id, String requestId, String timelineId, String role,
                String senderId, String text, String attachmentKind,
                String attachmentTranscript, long createdAt, String deliveryState) {
            this(id, requestId, "legacy", timelineId, role, senderId, text,
                    attachmentKind, attachmentTranscript, createdAt, deliveryState);
        }
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }
}
