package com.aion.chat.homecoming;

import android.content.Context;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;

import java.util.Arrays;
import java.util.Collections;
import java.util.List;

public final class HomecomingDatabase extends SQLiteOpenHelper {
    public static final String DATABASE_NAME = "homecoming.db";
    public static final int DATABASE_VERSION = 6;

    private static final List<String> SNAPSHOT_TABLE_NAMES =
            Collections.unmodifiableList(Arrays.asList(
            "snapshot_meta",
            "identity_snapshot",
            "timeline_snapshot",
            "message_snapshot",
            "memory_snapshot",
            "schedule_snapshot",
            "runtime_snapshot",
            "route_descriptor"
    ));
    private static final List<String> TABLE_NAMES =
            Collections.unmodifiableList(Arrays.asList(
                    "snapshot_meta",
                    "identity_snapshot",
                    "timeline_snapshot",
                    "message_snapshot",
                    "memory_snapshot",
                    "schedule_snapshot",
                    "runtime_snapshot",
                    "route_descriptor",
                    "homecoming_epoch",
                    "chat_message",
                    "chat_request",
                    "memory_local",
                    "summary_anchor",
                    "operation_journal",
                    "schedule_local",
                    "schedule_execution",
                    "supervision_event",
                    "return_package",
                    "return_attempt"
            ));

    private static final String[] SNAPSHOT_SQL = {
            "CREATE TABLE snapshot_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "CREATE TABLE identity_snapshot(identity_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)",
            "CREATE TABLE timeline_snapshot(timeline_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)",
            "CREATE TABLE message_snapshot(id TEXT PRIMARY KEY, timeline_id TEXT NOT NULL, payload_json TEXT NOT NULL)",
            "CREATE TABLE memory_snapshot(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, payload_json TEXT NOT NULL)",
            "CREATE TABLE schedule_snapshot(id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)",
            "CREATE TABLE runtime_snapshot(key TEXT PRIMARY KEY, payload_json TEXT NOT NULL)",
            "CREATE TABLE route_descriptor(route_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)"
    };
    private static final String[] WRITABLE_SQL = {
            "CREATE TABLE homecoming_epoch(epoch_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, "
                    + "started_at INTEGER NOT NULL, frozen_at INTEGER)",
            "CREATE TABLE chat_request(request_id TEXT PRIMARY KEY, timeline_id TEXT NOT NULL, "
                    + "state TEXT NOT NULL, failure_code TEXT, created_at INTEGER NOT NULL, "
                    + "updated_at INTEGER NOT NULL)",
            "CREATE TABLE chat_message(id TEXT PRIMARY KEY, request_id TEXT NOT NULL, "
                    + "epoch_id TEXT NOT NULL, "
                    + "timeline_id TEXT NOT NULL, role TEXT NOT NULL, sender_id TEXT NOT NULL, "
                    + "text_content TEXT NOT NULL, attachment_kind TEXT NOT NULL DEFAULT '', "
                    + "attachment_transcript TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, "
                    + "delivery_state TEXT NOT NULL, UNIQUE(request_id, role))",
            "CREATE INDEX idx_homecoming_chat_timeline_time "
                    + "ON chat_message(timeline_id, created_at, id)",
            "CREATE INDEX idx_homecoming_chat_epoch_timeline_time "
                    + "ON chat_message(epoch_id, timeline_id, created_at, id)",
            "CREATE TABLE memory_local(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, "
                    + "payload_json TEXT NOT NULL, base_hash TEXT NOT NULL DEFAULT '', "
                    + "tombstone INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL)",
            "CREATE TABLE summary_anchor(epoch_id TEXT NOT NULL, "
                    + "owner_id TEXT NOT NULL, last_message_id TEXT NOT NULL DEFAULT '', "
                    + "updated_at INTEGER NOT NULL, PRIMARY KEY(epoch_id, owner_id))",
            "CREATE TABLE operation_journal(op_id TEXT PRIMARY KEY, epoch_id TEXT NOT NULL, "
                    + "device_id TEXT NOT NULL, device_seq INTEGER NOT NULL UNIQUE, "
                    + "entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, action TEXT NOT NULL, "
                    + "base_revision TEXT NOT NULL, payload_json TEXT NOT NULL, "
                    + "created_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
                    + "server_receipt TEXT NOT NULL DEFAULT '')"
    };
    private static final String[] SCHEDULE_SQL = {
            "CREATE TABLE schedule_local(id TEXT PRIMARY KEY, type TEXT NOT NULL, "
                    + "trigger_at INTEGER NOT NULL, content TEXT NOT NULL, "
                    + "owner_id TEXT NOT NULL, timeline_id TEXT NOT NULL, "
                    + "base_hash TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, "
                    + "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
                    + "ended_at INTEGER)",
            "CREATE INDEX idx_homecoming_schedule_due "
                    + "ON schedule_local(status, trigger_at)",
            "CREATE TABLE schedule_execution(execution_id TEXT PRIMARY KEY, "
                    + "schedule_id TEXT NOT NULL, trigger_at INTEGER NOT NULL, "
                    + "state TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '', "
                    + "diagnostic TEXT NOT NULL DEFAULT '', started_at INTEGER NOT NULL, "
                    + "finished_at INTEGER, UNIQUE(schedule_id, trigger_at))"
    };
    private static final String[] SUPERVISION_SQL = {
            "CREATE TABLE supervision_event(event_id TEXT PRIMARY KEY, "
                    + "epoch_id TEXT NOT NULL, group_id TEXT NOT NULL, "
                    + "checkpoint_ms INTEGER NOT NULL, role_id TEXT NOT NULL, "
                    + "payload_json TEXT NOT NULL, state TEXT NOT NULL, "
                    + "attempt_count INTEGER NOT NULL DEFAULT 0, "
                    + "diagnostic TEXT NOT NULL DEFAULT '', "
                    + "message_id TEXT NOT NULL DEFAULT '', "
                    + "result_text TEXT NOT NULL DEFAULT '', "
                    + "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)",
            "CREATE INDEX idx_homecoming_supervision_recovery "
                    + "ON supervision_event(state, updated_at)"
    };
    private static final String[] RETURN_SQL = {
            "CREATE TABLE return_package(package_id TEXT PRIMARY KEY, "
                    + "epoch_id TEXT NOT NULL, device_id TEXT NOT NULL, "
                    + "base_snapshot_id TEXT NOT NULL, "
                    + "first_device_seq INTEGER NOT NULL, "
                    + "highest_device_seq INTEGER NOT NULL, "
                    + "operation_count INTEGER NOT NULL, "
                    + "payload_sha256 TEXT NOT NULL, signature_b64 TEXT NOT NULL, "
                    + "envelope_path TEXT NOT NULL, state TEXT NOT NULL, "
                    + "server_import_id TEXT NOT NULL DEFAULT '', "
                    + "result_summary_sha256 TEXT NOT NULL DEFAULT '', "
                    + "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)",
            "CREATE INDEX idx_homecoming_return_sequence "
                    + "ON return_package(state, first_device_seq)",
            "CREATE TABLE return_attempt(id TEXT PRIMARY KEY, "
                    + "package_id TEXT NOT NULL, stage TEXT NOT NULL, "
                    + "result_code TEXT NOT NULL, retryable INTEGER NOT NULL, "
                    + "diagnostic TEXT NOT NULL, created_at INTEGER NOT NULL)"
    };

    public static final String CREATE_STATEMENTS =
            joinStatements(SNAPSHOT_SQL) + ";\n" + joinStatements(WRITABLE_SQL)
                    + ";\n" + joinStatements(SCHEDULE_SQL)
                    + ";\n" + joinStatements(SUPERVISION_SQL)
                    + ";\n" + joinStatements(RETURN_SQL);

    public HomecomingDatabase(Context context) {
        super(context, DATABASE_NAME, null, DATABASE_VERSION);
    }

    @Override
    public void onCreate(SQLiteDatabase database) {
        for (String statement : SNAPSHOT_SQL) {
            database.execSQL(statement);
        }
        for (String statement : WRITABLE_SQL) {
            database.execSQL(statement);
        }
        for (String statement : SCHEDULE_SQL) {
            database.execSQL(statement);
        }
        for (String statement : SUPERVISION_SQL) {
            database.execSQL(statement);
        }
        for (String statement : RETURN_SQL) {
            database.execSQL(statement);
        }
    }

    @Override
    public void onUpgrade(SQLiteDatabase database, int oldVersion, int newVersion) {
        int current = oldVersion;
        if (current == 1 && newVersion >= 2) {
            for (String statement : WRITABLE_SQL) {
                database.execSQL(statement);
            }
            current = 2;
        }
        if (current == 2 && newVersion >= 3) {
            for (String statement : SCHEDULE_SQL) {
                database.execSQL(statement);
            }
            current = 3;
        }
        if (current == 3 && newVersion >= 4) {
            for (String statement : SUPERVISION_SQL) {
                database.execSQL(statement);
            }
            current = 4;
        }
        if (current == 4 && newVersion >= 5) {
            for (String statement : RETURN_SQL) {
                database.execSQL(statement);
            }
            current = 5;
        }
        if (current == 5 && newVersion >= 6) {
            database.execSQL(
                    "ALTER TABLE chat_message ADD COLUMN epoch_id TEXT NOT NULL DEFAULT 'legacy'");
            database.execSQL(
                    "CREATE INDEX idx_homecoming_chat_epoch_timeline_time "
                            + "ON chat_message(epoch_id, timeline_id, created_at, id)");
            database.execSQL(
                    "CREATE TABLE summary_anchor_v6(epoch_id TEXT NOT NULL, "
                            + "owner_id TEXT NOT NULL, last_message_id TEXT NOT NULL DEFAULT '', "
                            + "updated_at INTEGER NOT NULL, PRIMARY KEY(epoch_id, owner_id))");
            database.execSQL(
                    "INSERT INTO summary_anchor_v6(epoch_id,owner_id,last_message_id,updated_at) "
                            + "SELECT 'legacy',owner_id,last_message_id,updated_at "
                            + "FROM summary_anchor");
            database.execSQL("DROP TABLE summary_anchor");
            database.execSQL("ALTER TABLE summary_anchor_v6 RENAME TO summary_anchor");
            current = 6;
        }
        if (current == newVersion) {
            return;
        }
        throw new IllegalStateException("Homecoming database migration is not defined for "
                + oldVersion + " -> " + newVersion);
    }

    public static List<String> tableNames() {
        return TABLE_NAMES;
    }

    public static List<String> snapshotTableNames() {
        return SNAPSHOT_TABLE_NAMES;
    }

    private static String joinStatements(String[] statements) {
        StringBuilder result = new StringBuilder();
        for (String statement : statements) {
            if (result.length() > 0) {
                result.append(";\n");
            }
            result.append(statement);
        }
        return result.toString();
    }
}
