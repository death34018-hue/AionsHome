package com.aion.chat.homecoming;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.Collections;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

public class HomecomingBackupImportTest {
    @Test
    public void refreshRegistersSeparateReturnSigningKey() throws Exception {
        HomecomingSnapshotStore store = new HomecomingSnapshotStore(
                Files.createTempDirectory("homecoming-signing").toFile());
        FakeTransport transport = new FakeTransport(
                snapshotBody("snap-signing", 1, false), null);

        refresh(store, transport);

        assertEquals("fixture-signing-key", transport.signingPublicKey);
    }
    @Rule public TemporaryFolder folder = new TemporaryFolder();

    @Test
    public void hashMismatchKeepsOldActiveSnapshot() throws Exception {
        HomecomingSnapshotStore store = storeWithOldActive();
        byte[] body = snapshotBody("snap-new", 1, true);
        RecordingCallback callback = refresh(store, new FakeTransport(body, null));

        assertEquals("snap-old", store.activeManifest().snapshotId);
        assertEquals("HASH_MISMATCH", callback.failureCode);
    }

    @Test
    public void schemaMismatchKeepsOldActiveSnapshot() throws Exception {
        HomecomingSnapshotStore store = storeWithOldActive();
        byte[] body = snapshotBody("snap-new", 2, false);
        RecordingCallback callback = refresh(store, new FakeTransport(body, null));

        assertEquals("snap-old", store.activeManifest().snapshotId);
        assertEquals("SCHEMA_UNSUPPORTED", callback.failureCode);
    }

    @Test
    public void interruptedDownloadKeepsOldActiveSnapshot() throws Exception {
        HomecomingSnapshotStore store = storeWithOldActive();
        RecordingCallback callback = refresh(
                store, new FakeTransport(null, new IOException("interrupted")));

        assertEquals("snap-old", store.activeManifest().snapshotId);
        assertEquals("DOWNLOAD_INTERRUPTED", callback.failureCode);
    }

    @Test
    public void successfulImportReportsCountsAndActivatesOnlyAfterImport() throws Exception {
        HomecomingSnapshotStore store = storeWithOldActive();
        byte[] body = snapshotBody("snap-new", 1, false);
        RecordingImporter importer = new RecordingImporter();
        RecordingCallback callback = new RecordingCallback();
        HomecomingBackupClient client = new HomecomingBackupClient(
                store,
                new FakeTransport(body, null),
                new FakeKeys(),
                importer);

        client.refreshSnapshot(
                "https://server.example/chat",
                HomecomingBackupClient.RefreshReason.MANUAL,
                callback);

        assertNotNull(callback.success);
        assertEquals("snap-new", store.activeManifest().snapshotId);
        assertEquals("snap-new", importer.importedSnapshotId);
        assertEquals(12, callback.success.mainMemoryCount);
        assertEquals(9, callback.success.secondMemoryCount);
        assertEquals(700, callback.success.totalMessageCount());
        assertEquals(4, callback.success.pendingScheduleCount);
        assertEquals(3, callback.success.portableRouteCount);
    }

    @Test
    public void backupFailureDoesNotActivateHomecoming() throws Exception {
        HomecomingModeStore.Backend backend = new MemoryModeBackend();
        HomecomingModeStore modeStore = new HomecomingModeStore(backend);
        HomecomingSnapshotStore store = storeWithOldActive();

        refresh(store, new FakeTransport(null, new IOException("offline")));

        assertFalse(modeStore.isActive());
        assertEquals("", modeStore.currentEpoch());
    }

    private RecordingCallback refresh(
            HomecomingSnapshotStore store, HomecomingBackupClient.Transport transport) {
        RecordingCallback callback = new RecordingCallback();
        new HomecomingBackupClient(
                store, transport, new FakeKeys(), new RecordingImporter())
                .refreshSnapshot(
                        "https://server.example/chat",
                        HomecomingBackupClient.RefreshReason.MANUAL,
                        callback);
        return callback;
    }

    private HomecomingSnapshotStore storeWithOldActive() throws Exception {
        HomecomingSnapshotStore store = new HomecomingSnapshotStore(folder.getRoot());
        byte[] body = snapshotBody("snap-old", 1, false);
        install(store, body, "snap-old");
        assertTrue(store.activateStaging());
        return store;
    }

    private static void install(
            HomecomingSnapshotStore store, byte[] compressed, String snapshotId)
            throws Exception {
        JSONObject payload = HomecomingBackupClient.parseCompressedSnapshot(compressed);
        JSONObject manifest = HomecomingBackupClient.localManifest(payload, compressed);
        store.beginStaging(snapshotId);
        store.writeStagingFile("snapshot.json.gz", compressed);
        store.writeStagingFile(
                "manifest.json",
                HomecomingSnapshotStore.canonicalJson(manifest)
                        .getBytes(StandardCharsets.UTF_8));
        store.writeStagingFile("READY", new byte[0]);
    }

    private static byte[] snapshotBody(
            String snapshotId, int schema, boolean corruptIdentityHash) throws Exception {
        JSONObject memories = new JSONObject()
                .put("main", items(12))
                .put("second", items(9));
        JSONObject timelines = new JSONObject()
                .put("main_private", new JSONObject().put("messages", items(300)))
                .put("companion_private", new JSONObject().put("messages", items(220)))
                .put("group", new JSONObject().put("messages", items(180)));
        JSONObject sections = new JSONObject()
                .put("identity", new JSONObject().put("configured_name", "Fixture"))
                .put("memories", memories)
                .put("timelines", timelines)
                .put("schedules", items(4))
                .put("runtime_state", new JSONObject())
                .put("route_descriptors", new JSONObject());
        JSONObject hashes = new JSONObject();
        for (String name : HomecomingContract.SECTION_NAMES) {
            hashes.put(name, HomecomingSnapshotStore.sha256Hex(
                    HomecomingSnapshotStore.canonicalJson(sections.get(name))
                            .getBytes(StandardCharsets.UTF_8)));
        }
        if (corruptIdentityHash) {
            hashes.put("identity", "00");
        }
        JSONObject payload = new JSONObject()
                .put("schema", schema)
                .put("snapshot_id", snapshotId)
                .put("created_at", 1_800_000_000L)
                .put("sections", sections)
                .put("section_hashes", hashes)
                .put("encrypted_routes", new JSONObject());
        return HomecomingSnapshotStore.gzip(
                HomecomingSnapshotStore.canonicalJson(payload)
                        .getBytes(StandardCharsets.UTF_8));
    }

    private static JSONArray items(int count) throws Exception {
        JSONArray values = new JSONArray();
        for (int i = 0; i < count; i++) {
            values.put(new JSONObject().put("id", "item-" + i));
        }
        return values;
    }

    private static final class FakeTransport implements HomecomingBackupClient.Transport {
        private final byte[] body;
        private final IOException failure;
        String signingPublicKey;

        FakeTransport(byte[] body, IOException failure) {
            this.body = body;
            this.failure = failure;
        }

        @Override
        public HomecomingBackupClient.Download download(
                String serverBaseUrl, String deviceId, String publicKeySpki,
                String signingPublicKeySpki, String previousId)
                throws IOException {
            signingPublicKey = signingPublicKeySpki;
            if (failure != null) {
                throw failure;
            }
            return new HomecomingBackupClient.Download(200, body, "");
        }
    }

    private static final class FakeKeys implements HomecomingBackupClient.KeyPort {
        @Override public String deviceId() { return "android:test-device"; }
        @Override public String publicKeySpkiBase64() { return "fixture-public-key"; }
        @Override public String signingPublicKeySpkiBase64() {
            return "fixture-signing-key";
        }
        @Override
        public int decryptAndStoreRoutes(JSONObject envelope, String deviceId, String snapshotId) {
            return 3;
        }
    }

    private static final class RecordingImporter
            implements HomecomingBackupClient.SnapshotImporter {
        String importedSnapshotId;

        @Override
        public void importSnapshot(JSONObject payload) throws Exception {
            importedSnapshotId = payload.getString("snapshot_id");
        }
    }

    private static final class RecordingCallback implements HomecomingBackupClient.Callback {
        HomecomingReadiness success;
        String failureCode;

        @Override
        public void onSuccess(HomecomingReadiness readiness) {
            success = readiness;
        }

        @Override
        public void onFailure(String code, String diagnostic) {
            failureCode = code;
        }
    }

    private static final class MemoryModeBackend implements HomecomingModeStore.Backend {
        private final java.util.Map<String, Object> values = new java.util.LinkedHashMap<>();
        @Override public String getString(String key, String fallback) {
            Object value = values.get(key);
            return value instanceof String ? (String) value : fallback;
        }
        @Override public long getLong(String key, long fallback) {
            Object value = values.get(key);
            return value instanceof Long ? (Long) value : fallback;
        }
        @Override public void putString(String key, String value) { values.put(key, value); }
        @Override public void putLong(String key, long value) { values.put(key, value); }
        @Override public void remove(String key) { values.remove(key); }
    }
}
