package com.aion.chat.widget;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

public final class PrivateMemoStore extends SQLiteOpenHelper {
    private static final String DB_NAME = "widget_private_memos.db";
    private static final int DB_VERSION = 2;

    public PrivateMemoStore(Context context) {
        super(context.getApplicationContext(), DB_NAME, null, DB_VERSION);
    }

    @Override
    public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE private_memos ("
                + "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                + "server_id TEXT NOT NULL UNIQUE,"
                + "content TEXT NOT NULL,"
                + "status TEXT NOT NULL DEFAULT 'active',"
                + "source TEXT NOT NULL DEFAULT 'widget',"
                + "created_at INTEGER NOT NULL,"
                + "updated_at INTEGER NOT NULL,"
                + "sync_state TEXT NOT NULL DEFAULT 'pending')");
    }

    @Override
    public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        if (oldVersion < 2) {
            db.execSQL("ALTER TABLE private_memos ADD COLUMN server_id TEXT");
            db.execSQL("ALTER TABLE private_memos ADD COLUMN status TEXT NOT NULL DEFAULT 'active'");
            db.execSQL("ALTER TABLE private_memos ADD COLUMN source TEXT NOT NULL DEFAULT 'widget'");
            db.execSQL("ALTER TABLE private_memos ADD COLUMN sync_state TEXT NOT NULL DEFAULT 'pending'");
            db.execSQL("UPDATE private_memos SET server_id='legacy-' || id || '-' || lower(hex(randomblob(8))) WHERE server_id IS NULL");
            db.execSQL("CREATE UNIQUE INDEX IF NOT EXISTS idx_private_memos_server_id ON private_memos(server_id)");
        }
    }

    public long add(String content) {
        PrivateMemo memo = addPending(content, "widget");
        return memo == null ? -1L : memo.id;
    }

    public PrivateMemo addPending(String content, String source) {
        String value = content == null ? "" : content.trim();
        if (value.isEmpty()) return null;
        long now = System.currentTimeMillis();
        String serverId = UUID.randomUUID().toString();
        ContentValues values = new ContentValues();
        values.put("server_id", serverId);
        values.put("content", value);
        values.put("status", "active");
        values.put("source", source == null || source.trim().isEmpty() ? "widget" : source.trim());
        values.put("created_at", now);
        values.put("updated_at", now);
        values.put("sync_state", "pending");
        long id = getWritableDatabase().insertOrThrow("private_memos", null, values);
        return new PrivateMemo(id, serverId, value, "active", values.getAsString("source"),
                now, now, "pending");
    }

    public List<PrivateMemo> latest(int limit) {
        int safeLimit = Math.max(1, limit);
        List<PrivateMemo> result = new ArrayList<>();
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT id, content, created_at, updated_at FROM private_memos "
                        + "WHERE status='active' ORDER BY updated_at DESC, id DESC LIMIT ?",
                new String[]{String.valueOf(safeLimit)})) {
            while (cursor.moveToNext()) {
                result.add(new PrivateMemo(
                        cursor.getLong(0), "", cursor.getString(1), "active", "widget",
                        cursor.getLong(2), cursor.getLong(3), "synced"));
            }
        }
        return result;
    }

    public List<PrivateMemo> pending() {
        List<PrivateMemo> result = new ArrayList<>();
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT id, server_id, content, status, source, created_at, updated_at, sync_state "
                        + "FROM private_memos WHERE sync_state='pending' ORDER BY id",
                null)) {
            while (cursor.moveToNext()) result.add(readMemo(cursor));
        }
        return result;
    }

    public void markSynced(String serverId) {
        ContentValues values = new ContentValues();
        values.put("sync_state", "synced");
        getWritableDatabase().update("private_memos", values, "server_id=?",
                new String[]{serverId});
    }

    public void replaceSynced(List<PrivateMemo> serverMemos) {
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            db.delete("private_memos", "sync_state='synced'", null);
            for (PrivateMemo memo : serverMemos) {
                ContentValues values = new ContentValues();
                values.put("server_id", memo.serverId);
                values.put("content", memo.content);
                values.put("status", memo.status);
                values.put("source", memo.source);
                values.put("created_at", memo.createdAt);
                values.put("updated_at", memo.updatedAt);
                values.put("sync_state", "synced");
                db.insertWithOnConflict("private_memos", null, values,
                        SQLiteDatabase.CONFLICT_REPLACE);
            }
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    private static PrivateMemo readMemo(Cursor cursor) {
        return new PrivateMemo(cursor.getLong(0), cursor.getString(1), cursor.getString(2),
                cursor.getString(3), cursor.getString(4), cursor.getLong(5),
                cursor.getLong(6), cursor.getString(7));
    }
}
