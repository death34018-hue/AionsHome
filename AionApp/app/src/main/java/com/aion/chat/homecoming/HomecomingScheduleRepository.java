package com.aion.chat.homecoming;

import android.content.ContentValues;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;

import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.text.ParseException;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.TimeZone;
import java.util.UUID;

public final class HomecomingScheduleRepository {
    private static final long STALE_GRACE_MS = 90_000L;
    private static final long CLAIM_RECOVERY_MS = 300_000L;
    private static final int MAX_CONTENT_LENGTH = 2_000;

    private final Backend backend;
    private final String epochId;
    private final String deviceId;

    public HomecomingScheduleRepository(
            HomecomingDatabase database, String epochId, String deviceId) {
        this(new SQLiteBackend(database), epochId, deviceId);
    }

    HomecomingScheduleRepository(Backend backend, String epochId, String deviceId) {
        if (backend == null) throw new IllegalArgumentException("backend is required");
        this.backend = backend;
        this.epochId = required(epochId, "epochId");
        this.deviceId = required(deviceId, "deviceId");
    }

    public List<Schedule> listActive() {
        ArrayList<Schedule> active = new ArrayList<>();
        for (Schedule schedule : merged().values()) {
            if ("active".equals(schedule.status)) active.add(schedule);
        }
        active.sort(Comparator.comparingLong((Schedule row) -> row.triggerAt)
                .thenComparing(row -> row.id));
        return Collections.unmodifiableList(active);
    }

    public Schedule find(String id) {
        return merged().get(required(id, "id"));
    }

    public Schedule create(
            String type, long triggerAt, String content, String ownerId,
            String timelineId, long now) {
        return createWithId(
                UUID.randomUUID().toString(), type, triggerAt, content,
                ownerId, timelineId, now);
    }

    Schedule createWithId(
            String id, String type, long triggerAt, String content, String ownerId,
            String timelineId, long now) {
        String checkedId = required(id, "id");
        Schedule existing = find(checkedId);
        if (existing != null) return existing;
        String checkedType = validType(type);
        String checkedContent = validContent(content);
        String checkedOwner = validOwner(ownerId);
        String checkedTimeline = validTimeline(timelineId);
        validTrigger(triggerAt, now);
        Schedule candidate = new Schedule(
                checkedId,
                checkedType,
                triggerAt,
                checkedContent,
                checkedOwner,
                checkedTimeline,
                "",
                "active",
                now,
                now,
                null);
        return backend.writeLocal(candidate, operation(candidate, "create", now));
    }

    public Schedule delete(String id, long now) {
        Schedule current = find(id);
        if (current == null || !"active".equals(current.status)) {
            throw new IllegalArgumentException("active schedule is required");
        }
        Schedule deleted = new Schedule(
                current.id, current.type, current.triggerAt, current.content,
                current.ownerId, current.timelineId, current.baseHash,
                "deleted", current.createdAt, now, now);
        return backend.writeLocal(deleted, operation(deleted, "delete", now));
    }

    public Schedule markMissed(String id, long now) {
        Schedule current = find(id);
        if (current == null || !"active".equals(current.status)) {
            return current;
        }
        Schedule missed = new Schedule(
                current.id, current.type, current.triggerAt, current.content,
                current.ownerId, current.timelineId, current.baseHash,
                "missed", current.createdAt, now, now);
        return backend.writeLocal(missed, operation(missed, "execute", now));
    }

    public ExecutionClaim claimExecution(
            String scheduleId, long triggerAt, long now) {
        Schedule schedule = find(scheduleId);
        if (schedule == null || !"active".equals(schedule.status)) {
            throw new IllegalArgumentException("active schedule is required");
        }
        if (schedule.triggerAt != triggerAt) {
            throw new IllegalArgumentException("trigger does not match schedule");
        }
        String executionId = schedule.id + ":" + triggerAt;
        return backend.claim(executionId, schedule.id, triggerAt, now);
    }

    public void completeExecution(String executionId, String messageId, long now) {
        String checkedExecution = required(executionId, "executionId");
        Execution execution = backend.execution(checkedExecution);
        if (execution == null) throw new IllegalStateException("unknown execution");
        if ("complete".equals(execution.state)) return;
        Schedule schedule = find(execution.scheduleId);
        if (schedule == null) throw new IllegalStateException("unknown schedule");
        JSONObject payload = new JSONObject();
        try {
            payload.put("schedule_id", schedule.id);
            payload.put("execution_id", checkedExecution);
            payload.put("trigger_at", execution.triggerAt);
            payload.put("message_id", required(messageId, "messageId"));
            payload.put("executed_at", now);
        } catch (Exception exception) {
            throw new IllegalStateException("could not encode execution", exception);
        }
        HomecomingOperationJournal.Operation operation =
                HomecomingOperationJournal.create(
                        epochId, deviceId, "schedule", schedule.id, "execute",
                        schedule.baseHash,
                        HomecomingSnapshotStore.canonicalJson(payload),
                        now);
        Schedule completed = new Schedule(
                schedule.id, schedule.type, schedule.triggerAt, schedule.content,
                schedule.ownerId, schedule.timelineId, schedule.baseHash,
                "completed", schedule.createdAt, now, now);
        backend.complete(checkedExecution, messageId, now, completed, operation);
    }

    public void failExecution(String executionId, String diagnostic, long now) {
        String compact = diagnostic == null ? "" :
                diagnostic.replace('\n', ' ').replace('\r', ' ').trim();
        if (compact.length() > 240) compact = compact.substring(0, 240);
        backend.fail(required(executionId, "executionId"), compact, now);
    }

    private Map<String, Schedule> merged() {
        LinkedHashMap<String, Schedule> values = new LinkedHashMap<>();
        for (Schedule schedule : backend.snapshotSchedules()) {
            values.put(schedule.id, schedule);
        }
        for (Schedule schedule : backend.localSchedules()) {
            values.put(schedule.id, schedule);
        }
        return values;
    }

    private HomecomingOperationJournal.Operation operation(
            Schedule schedule, String action, long now) {
        return HomecomingOperationJournal.create(
                epochId, deviceId, "schedule", schedule.id, action,
                schedule.baseHash, payload(schedule), now);
    }

    private static String payload(Schedule schedule) {
        try {
            JSONObject value = new JSONObject()
                    .put("id", schedule.id)
                    .put("type", schedule.type)
                    .put("trigger_at", schedule.triggerAt)
                    .put("content", schedule.content)
                    .put("owner_id", schedule.ownerId)
                    .put("timeline_id", schedule.timelineId)
                    .put("status", schedule.status)
                    .put("updated_at", schedule.updatedAt);
            if (schedule.endedAt != null) value.put("ended_at", schedule.endedAt);
            return HomecomingSnapshotStore.canonicalJson(value);
        } catch (Exception exception) {
            throw new IllegalStateException("could not encode schedule", exception);
        }
    }

    private static String validType(String value) {
        String type = required(value, "type").toLowerCase(Locale.ROOT);
        if (!"alarm".equals(type) && !"reminder".equals(type)
                && !"monitor".equals(type)) {
            throw new IllegalArgumentException("unsupported schedule type");
        }
        return type;
    }

    private static String validOwner(String value) {
        String owner = required(value, "ownerId");
        if (!"main".equals(owner) && !"second".equals(owner)) {
            throw new IllegalArgumentException("unsupported owner");
        }
        return owner;
    }

    private static String validTimeline(String value) {
        String timeline = required(value, "timelineId");
        if (!"main_private".equals(timeline)
                && !"companion_private".equals(timeline)
                && !"group".equals(timeline)) {
            throw new IllegalArgumentException("unsupported timeline");
        }
        return timeline;
    }

    private static String validContent(String value) {
        String content = required(value, "content");
        if (content.length() > MAX_CONTENT_LENGTH) {
            throw new IllegalArgumentException("content is too long");
        }
        return content;
    }

    private static void validTrigger(long triggerAt, long now) {
        if (triggerAt < now - STALE_GRACE_MS) {
            throw new IllegalArgumentException("schedule time is stale");
        }
    }

    interface Backend {
        List<Schedule> snapshotSchedules();
        List<Schedule> localSchedules();
        Schedule writeLocal(
                Schedule schedule, HomecomingOperationJournal.Operation operation);
        ExecutionClaim claim(
                String executionId, String scheduleId, long triggerAt, long now);
        Execution execution(String executionId);
        void complete(
                String executionId, String messageId, long now,
                Schedule completedSchedule,
                HomecomingOperationJournal.Operation operation);
        void fail(String executionId, String diagnostic, long now);
    }

    private static final class SQLiteBackend implements Backend {
        private final HomecomingDatabase helper;

        SQLiteBackend(HomecomingDatabase helper) {
            this.helper = helper;
        }

        @Override
        public List<Schedule> snapshotSchedules() {
            ArrayList<Schedule> values = new ArrayList<>();
            try (Cursor cursor = helper.getReadableDatabase().rawQuery(
                    "SELECT id,payload_json FROM schedule_snapshot", null)) {
                while (cursor.moveToNext()) {
                    try {
                        values.add(snapshot(cursor.getString(0),
                                new JSONObject(cursor.getString(1))));
                    } catch (Exception ignored) {
                        // An invalid snapshot row is unavailable, never invented.
                    }
                }
            }
            return values;
        }

        @Override
        public List<Schedule> localSchedules() {
            ArrayList<Schedule> values = new ArrayList<>();
            try (Cursor cursor = helper.getReadableDatabase().rawQuery(
                    "SELECT id,type,trigger_at,content,owner_id,timeline_id,"
                            + "base_hash,status,created_at,updated_at,ended_at "
                            + "FROM schedule_local", null)) {
                while (cursor.moveToNext()) values.add(readSchedule(cursor));
            }
            return values;
        }

        @Override
        public Schedule writeLocal(
                Schedule schedule, HomecomingOperationJournal.Operation operation) {
            SQLiteDatabase database = helper.getWritableDatabase();
            database.beginTransaction();
            try {
                ContentValues values = scheduleValues(schedule);
                database.insertWithOnConflict(
                        "schedule_local", null, values, SQLiteDatabase.CONFLICT_REPLACE);
                insertOperation(database, operation);
                database.setTransactionSuccessful();
                return schedule;
            } finally {
                database.endTransaction();
            }
        }

        @Override
        public ExecutionClaim claim(
                String executionId, String scheduleId, long triggerAt, long now) {
            SQLiteDatabase database = helper.getWritableDatabase();
            ContentValues values = new ContentValues();
            values.put("execution_id", executionId);
            values.put("schedule_id", scheduleId);
            values.put("trigger_at", triggerAt);
            values.put("state", "claimed");
            values.put("started_at", now);
            long inserted = database.insertWithOnConflict(
                    "schedule_execution", null, values, SQLiteDatabase.CONFLICT_IGNORE);
            Execution current = execution(executionId);
            if (inserted == -1 && current != null
                    && ("failed".equals(current.state)
                    || ("claimed".equals(current.state)
                    && now - current.startedAt > CLAIM_RECOVERY_MS))) {
                ContentValues recovered = new ContentValues();
                recovered.put("state", "claimed");
                recovered.put("message_id", "");
                recovered.put("diagnostic", "");
                recovered.put("started_at", now);
                recovered.putNull("finished_at");
                database.update(
                        "schedule_execution",
                        recovered,
                        "execution_id=?",
                        new String[]{executionId});
                return new ExecutionClaim(
                        executionId, scheduleId, triggerAt, true, "claimed");
            }
            return new ExecutionClaim(
                    executionId, scheduleId, triggerAt, inserted != -1,
                    current == null ? "" : current.state);
        }

        @Override
        public Execution execution(String executionId) {
            try (Cursor cursor = helper.getReadableDatabase().rawQuery(
                    "SELECT execution_id,schedule_id,trigger_at,state,message_id,"
                            + "diagnostic,started_at,finished_at "
                            + "FROM schedule_execution WHERE execution_id=?",
                    new String[]{executionId})) {
                return cursor.moveToFirst() ? readExecution(cursor) : null;
            }
        }

        @Override
        public void complete(
                String executionId, String messageId, long now,
                Schedule completedSchedule,
                HomecomingOperationJournal.Operation operation) {
            SQLiteDatabase database = helper.getWritableDatabase();
            database.beginTransaction();
            try {
                Execution current = execution(executionId);
                if (current == null) throw new IllegalStateException("unknown execution");
                if ("complete".equals(current.state)) {
                    database.setTransactionSuccessful();
                    return;
                }
                ContentValues values = new ContentValues();
                values.put("state", "complete");
                values.put("message_id", required(messageId, "messageId"));
                values.put("diagnostic", "");
                values.put("finished_at", now);
                database.update("schedule_execution", values, "execution_id=?",
                        new String[]{executionId});
                database.insertWithOnConflict(
                        "schedule_local",
                        null,
                        scheduleValues(completedSchedule),
                        SQLiteDatabase.CONFLICT_REPLACE);
                insertOperation(database, operation);
                database.setTransactionSuccessful();
            } finally {
                database.endTransaction();
            }
        }

        @Override
        public void fail(String executionId, String diagnostic, long now) {
            ContentValues values = new ContentValues();
            values.put("state", "failed");
            values.put("diagnostic", diagnostic);
            values.put("finished_at", now);
            int changed = helper.getWritableDatabase().update(
                    "schedule_execution", values, "execution_id=?",
                    new String[]{executionId});
            if (changed == 0) throw new IllegalStateException("unknown execution");
        }

        private static Schedule snapshot(String id, JSONObject value) throws Exception {
            String canonical = HomecomingSnapshotStore.canonicalJson(value);
            String baseHash = HomecomingSnapshotStore.sha256Hex(
                    canonical.getBytes(StandardCharsets.UTF_8));
            String origin = value.optString("origin", "aion");
            String owner = "connor".equalsIgnoreCase(origin) ? "second" : "main";
            String originRoom = value.optString("origin_room_id", "");
            String timeline = originRoom.isEmpty()
                    ? ("second".equals(owner) ? "companion_private" : "main_private")
                    : "group";
            long createdAt = portableTime(value.opt("created_at"));
            Long endedAt = value.isNull("ended_at")
                    ? null : portableTime(value.opt("ended_at"));
            return new Schedule(
                    id,
                    validType(value.getString("type")),
                    portableTime(value.get("trigger_at")),
                    validContent(value.getString("content")),
                    owner,
                    timeline,
                    baseHash,
                    value.optString("status", "active"),
                    createdAt,
                    createdAt,
                    endedAt);
        }

        private static long portableTime(Object value) throws ParseException {
            if (value instanceof Number) {
                double raw = ((Number) value).doubleValue();
                return raw < 10_000_000_000L ? (long) (raw * 1000D) : (long) raw;
            }
            String text = String.valueOf(value).trim().replace('T', ' ');
            String[] formats = {
                    "yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd HH:mm",
                    "yyyy/MM/dd HH:mm:ss", "yyyy/MM/dd HH:mm"
            };
            for (String format : formats) {
                SimpleDateFormat parser = new SimpleDateFormat(format, Locale.ROOT);
                parser.setLenient(false);
                parser.setTimeZone(TimeZone.getDefault());
                try {
                    Date parsed = parser.parse(text);
                    if (parsed != null) return parsed.getTime();
                } catch (ParseException ignored) {
                }
            }
            throw new ParseException("unsupported schedule time", 0);
        }

        private static ContentValues scheduleValues(Schedule schedule) {
            ContentValues values = new ContentValues();
            values.put("id", schedule.id);
            values.put("type", schedule.type);
            values.put("trigger_at", schedule.triggerAt);
            values.put("content", schedule.content);
            values.put("owner_id", schedule.ownerId);
            values.put("timeline_id", schedule.timelineId);
            values.put("base_hash", schedule.baseHash);
            values.put("status", schedule.status);
            values.put("created_at", schedule.createdAt);
            values.put("updated_at", schedule.updatedAt);
            if (schedule.endedAt == null) values.putNull("ended_at");
            else values.put("ended_at", schedule.endedAt);
            return values;
        }

        private static Schedule readSchedule(Cursor cursor) {
            return new Schedule(
                    cursor.getString(0), cursor.getString(1), cursor.getLong(2),
                    cursor.getString(3), cursor.getString(4), cursor.getString(5),
                    cursor.getString(6), cursor.getString(7), cursor.getLong(8),
                    cursor.getLong(9), cursor.isNull(10) ? null : cursor.getLong(10));
        }

        private static Execution readExecution(Cursor cursor) {
            return new Execution(
                    cursor.getString(0), cursor.getString(1), cursor.getLong(2),
                    cursor.getString(3), cursor.getString(4), cursor.getString(5),
                    cursor.getLong(6), cursor.isNull(7) ? null : cursor.getLong(7));
        }

        private static void insertOperation(
                SQLiteDatabase database, HomecomingOperationJournal.Operation operation) {
            long sequence = 1L;
            try (Cursor cursor = database.rawQuery(
                    "SELECT COALESCE(MAX(device_seq),0)+1 FROM operation_journal", null)) {
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

    public static final class Schedule {
        public final String id;
        public final String type;
        public final long triggerAt;
        public final String content;
        public final String ownerId;
        public final String timelineId;
        public final String baseHash;
        public final String status;
        public final long createdAt;
        public final long updatedAt;
        public final Long endedAt;

        Schedule(
                String id, String type, long triggerAt, String content,
                String ownerId, String timelineId, String baseHash, String status,
                long createdAt, long updatedAt, Long endedAt) {
            this.id = id;
            this.type = type;
            this.triggerAt = triggerAt;
            this.content = content;
            this.ownerId = ownerId;
            this.timelineId = timelineId;
            this.baseHash = baseHash == null ? "" : baseHash;
            this.status = status;
            this.createdAt = createdAt;
            this.updatedAt = updatedAt;
            this.endedAt = endedAt;
        }
    }

    public static final class ExecutionClaim {
        public final String executionId;
        public final String scheduleId;
        public final long triggerAt;
        public final boolean claimed;
        public final String state;

        ExecutionClaim(
                String executionId, String scheduleId, long triggerAt,
                boolean claimed, String state) {
            this.executionId = executionId;
            this.scheduleId = scheduleId;
            this.triggerAt = triggerAt;
            this.claimed = claimed;
            this.state = state;
        }
    }

    static final class Execution {
        final String executionId;
        final String scheduleId;
        final long triggerAt;
        final String state;
        final String messageId;
        final String diagnostic;
        final long startedAt;
        final Long finishedAt;

        Execution(
                String executionId, String scheduleId, long triggerAt, String state,
                String messageId, String diagnostic, long startedAt, Long finishedAt) {
            this.executionId = executionId;
            this.scheduleId = scheduleId;
            this.triggerAt = triggerAt;
            this.state = state;
            this.messageId = messageId;
            this.diagnostic = diagnostic;
            this.startedAt = startedAt;
            this.finishedAt = finishedAt;
        }
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }
}
