package com.aion.chat.homecoming;

import android.content.ContentValues;
import android.database.sqlite.SQLiteDatabase;

import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Iterator;
import java.util.concurrent.TimeUnit;
import java.util.zip.GZIPInputStream;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import okhttp3.ResponseBody;

public final class HomecomingBackupClient {
    static final int MAX_COMPRESSED_BYTES = 64 * 1024 * 1024;
    static final int MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024;
    private static final Object REFRESH_LOCK = new Object();

    public enum RefreshReason {
        FOREGROUND,
        ROUTE_SWITCH,
        CONFIG_EVENT,
        PERIODIC_FALLBACK,
        MANUAL
    }

    private final HomecomingSnapshotStore snapshotStore;
    private final Transport transport;
    private final KeyPort keys;
    private final SnapshotImporter importer;

    HomecomingBackupClient(
            HomecomingSnapshotStore snapshotStore,
            Transport transport,
            KeyPort keys,
            SnapshotImporter importer) {
        this.snapshotStore = snapshotStore;
        this.transport = transport;
        this.keys = keys;
        this.importer = importer;
    }

    public void refreshSnapshot(
            String serverBaseUrl, RefreshReason reason, Callback callback) {
        synchronized (REFRESH_LOCK) {
            refreshSnapshotLocked(serverBaseUrl, reason, callback);
        }
    }

    private void refreshSnapshotLocked(
            String serverBaseUrl, RefreshReason reason, Callback callback) {
        String previousId = snapshotStore.activeManifest() == null
                ? "" : snapshotStore.activeManifest().snapshotId;
        String stage = "download";
        try {
            Download download = transport.download(
                    serverBaseUrl,
                    keys.deviceId(),
                    keys.publicKeySpkiBase64(),
                    keys.signingPublicKeySpkiBase64(),
                    previousId);
            if (download.statusCode == 304) {
                callback.onSuccess(null);
                return;
            }
            if (download.statusCode != 200) {
                callback.onFailure("SERVER_ERROR", "snapshot HTTP " + download.statusCode);
                return;
            }
            if (download.body == null || download.body.length == 0
                    || download.body.length > MAX_COMPRESSED_BYTES) {
                callback.onFailure("DOWNLOAD_INVALID", "snapshot size is invalid");
                return;
            }
            stage = "parse";
            JSONObject payload = parseCompressedSnapshot(download.body);
            int schema = payload.getInt("schema");
            if (schema != HomecomingContract.SCHEMA_VERSION) {
                callback.onFailure("SCHEMA_UNSUPPORTED", "snapshot schema " + schema);
                return;
            }
            String snapshotId = payload.getString("snapshot_id");
            JSONObject manifest = localManifest(payload, download.body);
            stage = "stage";
            snapshotStore.beginStaging(snapshotId);
            snapshotStore.writeStagingFile("snapshot.json.gz", download.body);
            snapshotStore.writeStagingFile(
                    "manifest.json",
                    HomecomingSnapshotStore.canonicalJson(manifest)
                            .getBytes(StandardCharsets.UTF_8));
            snapshotStore.writeStagingFile("READY", new byte[0]);
            if (!snapshotStore.verifyStaging()) {
                snapshotStore.discardStaging();
                callback.onFailure("HASH_MISMATCH", "snapshot verification failed");
                return;
            }
            stage = "decrypt_routes";
            int routeCount = keys.decryptAndStoreRoutes(
                    payload.getJSONObject("encrypted_routes"),
                    keys.deviceId(),
                    snapshotId);
            stage = "import_database";
            importer.importSnapshot(payload);
            stage = "activate";
            if (!snapshotStore.activateStaging()) {
                callback.onFailure("ACTIVATION_FAILED", "snapshot rotation failed");
                return;
            }
            callback.onSuccess(HomecomingReadiness.fromPayload(
                    payload, routeCount, System.currentTimeMillis()));
        } catch (IOException exception) {
            logFailure("snapshot download or staging failed", exception);
            snapshotStore.discardStaging();
            callback.onFailure(
                    "DOWNLOAD_INTERRUPTED", safeDiagnostic(stage, exception));
        } catch (Exception exception) {
            logFailure("snapshot decrypt or import failed", exception);
            snapshotStore.discardStaging();
            callback.onFailure("IMPORT_FAILED", safeDiagnostic(stage, exception));
        }
    }

    private static void logFailure(String message, Throwable failure) {
        try {
            android.util.Log.e("HomecomingBackup", message, failure);
        } catch (RuntimeException ignored) {
            // android.util.Log is unavailable in local JVM unit tests.
        }
    }

    static JSONObject parseCompressedSnapshot(byte[] compressed) throws Exception {
        if (compressed.length > MAX_COMPRESSED_BYTES) {
            throw new IOException("compressed snapshot exceeds limit");
        }
        GZIPInputStream input = new GZIPInputStream(new ByteArrayInputStream(compressed));
        byte[] decompressed = readCapped(input, MAX_DECOMPRESSED_BYTES);
        Object value = new JSONTokener(
                new String(decompressed, StandardCharsets.UTF_8)).nextValue();
        if (!(value instanceof JSONObject)) {
            throw new IOException("snapshot root is not an object");
        }
        return (JSONObject) value;
    }

    static JSONObject localManifest(JSONObject payload, byte[] compressed) throws Exception {
        return new JSONObject()
                .put("schema", payload.getInt("schema"))
                .put("snapshot_id", payload.getString("snapshot_id"))
                .put("file_sha256", HomecomingSnapshotStore.sha256Hex(compressed))
                .put("section_hashes", payload.getJSONObject("section_hashes"));
    }

    private static byte[] readCapped(InputStream input, int limit) throws IOException {
        try {
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            byte[] buffer = new byte[8192];
            int total = 0;
            int count;
            while ((count = input.read(buffer)) != -1) {
                total += count;
                if (total > limit) {
                    throw new IOException("snapshot exceeds size limit");
                }
                output.write(buffer, 0, count);
            }
            return output.toByteArray();
        } finally {
            input.close();
        }
    }

    private static String safeDiagnostic(String stage, Exception exception) {
        String message = exception.getMessage();
        return stage + ":" + exception.getClass().getSimpleName()
                + (message == null ? "" : ":" + message);
    }

    interface Transport {
        Download download(
                String serverBaseUrl, String deviceId, String publicKeySpki,
                String signingPublicKeySpki, String previousId)
                throws IOException;
    }

    interface KeyPort {
        String deviceId();
        String publicKeySpkiBase64() throws Exception;
        String signingPublicKeySpkiBase64() throws Exception;
        int decryptAndStoreRoutes(
                JSONObject envelope, String deviceId, String snapshotId) throws Exception;
    }

    interface SnapshotImporter {
        void importSnapshot(JSONObject payload) throws Exception;
    }

    public interface Callback {
        void onSuccess(HomecomingReadiness readiness);
        void onFailure(String code, String diagnostic);
    }

    static final class Download {
        final int statusCode;
        final byte[] body;
        final String etag;

        Download(int statusCode, byte[] body, String etag) {
            this.statusCode = statusCode;
            this.body = body;
            this.etag = etag == null ? "" : etag;
        }
    }

    public static final class OkHttpTransport implements Transport {
        private static final MediaType JSON =
                MediaType.get("application/json; charset=utf-8");
        private final OkHttpClient client = new OkHttpClient.Builder()
                .connectTimeout(10, TimeUnit.SECONDS)
                .readTimeout(120, TimeUnit.SECONDS)
                .writeTimeout(30, TimeUnit.SECONDS)
                .build();

        @Override
        public Download download(
                String serverPageUrl, String deviceId, String publicKeySpki,
                String signingPublicKeySpki, String previousId)
                throws IOException {
            okhttp3.HttpUrl page = okhttp3.HttpUrl.parse(serverPageUrl);
            if (page == null || (!"https".equals(page.scheme()) && !"http".equals(page.scheme()))) {
                throw new IOException("invalid server URL");
            }
            String root = page.newBuilder().encodedPath("/").query(null).fragment(null)
                    .build().toString();
            JSONObject registration = new JSONObject();
            JSONObject buildBody = new JSONObject();
            try {
                registration.put("device_id", deviceId);
                registration.put("public_key_spki_b64", publicKeySpki);
                registration.put(
                        "signing_public_key_spki_b64", signingPublicKeySpki);
                buildBody.put("device_id", deviceId);
                buildBody.put("previous_snapshot_id",
                        previousId == null || previousId.isEmpty()
                                ? JSONObject.NULL : previousId);
            } catch (org.json.JSONException exception) {
                throw new IOException("could not encode snapshot request", exception);
            }
            Request register = new Request.Builder()
                    .url(root + "api/homecoming/v1/devices/register")
                    .post(RequestBody.create(registration.toString(), JSON))
                    .build();
            try (Response response = client.newCall(register).execute()) {
                if (!response.isSuccessful()) {
                    return new Download(response.code(), null, "");
                }
            }

            Request.Builder request = new Request.Builder()
                    .url(root + "api/homecoming/v1/snapshot/build")
                    .header("Accept-Encoding", "identity")
                    .post(RequestBody.create(buildBody.toString(), JSON));
            if (previousId != null && !previousId.isEmpty()) {
                request.header("If-None-Match", "\"" + previousId + "\"");
            }
            try (Response response = client.newCall(request.build()).execute()) {
                if (response.code() == 304) {
                    return new Download(304, null, response.header("ETag", ""));
                }
                ResponseBody responseBody = response.body();
                if (!response.isSuccessful() || responseBody == null) {
                    return new Download(response.code(), null, response.header("ETag", ""));
                }
                byte[] body = readCapped(responseBody.byteStream(), MAX_COMPRESSED_BYTES);
                return new Download(response.code(), body, response.header("ETag", ""));
            }
        }
    }

    public static final class DatabaseSnapshotImporter implements SnapshotImporter {
        private final HomecomingDatabase helper;

        public DatabaseSnapshotImporter(HomecomingDatabase helper) {
            this.helper = helper;
        }

        @Override
        public void importSnapshot(JSONObject payload) throws Exception {
            SQLiteDatabase database = helper.getWritableDatabase();
            database.beginTransaction();
            try {
                for (String table : HomecomingDatabase.snapshotTableNames()) {
                    database.delete(table, null, null);
                }
                putMeta(database, "snapshot_id", payload.getString("snapshot_id"));
                putMeta(database, "schema", String.valueOf(payload.getInt("schema")));
                putMeta(database, "created_at", String.valueOf(payload.optLong("created_at", 0L)));
                JSONObject sections = payload.getJSONObject("sections");
                insertObject(database, "identity_snapshot", "identity_id",
                        "identity", sections.getJSONObject("identity"));
                insertTimelines(database, sections.getJSONObject("timelines"));
                insertMemories(database, sections.getJSONObject("memories"));
                insertArray(database, "schedule_snapshot", "id",
                        sections.optJSONArray("schedules"), "");
                insertObject(database, "runtime_snapshot", "key",
                        "runtime", sections.getJSONObject("runtime_state"));
                JSONObject descriptors = sections.optJSONObject("route_descriptors");
                if (descriptors != null) {
                    Iterator<String> names = descriptors.keys();
                    while (names.hasNext()) {
                        String name = names.next();
                        insertObject(database, "route_descriptor", "route_id",
                                name, descriptors.get(name));
                    }
                }
                database.setTransactionSuccessful();
            } finally {
                database.endTransaction();
            }
        }

        private static void insertTimelines(SQLiteDatabase database, JSONObject timelines)
                throws Exception {
            Iterator<String> names = timelines.keys();
            while (names.hasNext()) {
                String timelineId = names.next();
                JSONObject timeline = timelines.getJSONObject(timelineId);
                insertObject(database, "timeline_snapshot", "timeline_id",
                        timelineId, timeline);
                JSONArray messages = timeline.optJSONArray("messages");
                if (messages != null) {
                    for (int i = 0; i < messages.length(); i++) {
                        JSONObject message = messages.getJSONObject(i);
                        ContentValues values = new ContentValues();
                        values.put("id", stableId(message, timelineId + ":" + i));
                        values.put("timeline_id", timelineId);
                        values.put("payload_json", message.toString());
                        database.insertOrThrow("message_snapshot", null, values);
                    }
                }
            }
        }

        private static void insertMemories(SQLiteDatabase database, JSONObject memories)
                throws Exception {
            Iterator<String> owners = memories.keys();
            while (owners.hasNext()) {
                String owner = owners.next();
                JSONArray values = memories.optJSONArray(owner);
                if (values == null) {
                    continue;
                }
                for (int i = 0; i < values.length(); i++) {
                    JSONObject memory = values.getJSONObject(i);
                    ContentValues row = new ContentValues();
                    row.put("id", stableId(memory, owner + ":" + i));
                    row.put("owner_id", owner);
                    row.put("payload_json", memory.toString());
                    database.insertOrThrow("memory_snapshot", null, row);
                }
            }
        }

        private static void insertArray(SQLiteDatabase database, String table,
                String idColumn, JSONArray values, String owner) throws Exception {
            if (values == null) {
                return;
            }
            for (int i = 0; i < values.length(); i++) {
                JSONObject value = values.getJSONObject(i);
                insertObject(database, table, idColumn,
                        stableId(value, owner + ":" + i), value);
            }
        }

        private static void insertObject(SQLiteDatabase database, String table,
                String idColumn, String id, Object payload) {
            ContentValues values = new ContentValues();
            values.put(idColumn, id);
            values.put("payload_json", String.valueOf(payload));
            database.insertOrThrow(table, null, values);
        }

        private static void putMeta(SQLiteDatabase database, String key, String value) {
            ContentValues row = new ContentValues();
            row.put("key", key);
            row.put("value", value);
            database.insertOrThrow("snapshot_meta", null, row);
        }

        private static String stableId(JSONObject value, String fallback) {
            String id = value.optString("id", "");
            return id.isEmpty() ? fallback : id;
        }
    }
}
