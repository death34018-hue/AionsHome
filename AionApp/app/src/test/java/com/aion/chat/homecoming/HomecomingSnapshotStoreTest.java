package com.aion.chat.homecoming;

import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.Collections;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

public class HomecomingSnapshotStoreTest {
    @Rule public TemporaryFolder folder = new TemporaryFolder();

    @Test
    public void incompleteStagingCannotReplaceActive() throws Exception {
        HomecomingSnapshotStore store = new HomecomingSnapshotStore(folder.getRoot());
        writeVerifiedSnapshot(store, "active", "active-old");
        store.beginStaging("active-new");

        assertFalse(store.activateStaging());
        assertEquals("active-old", store.activeManifest().snapshotId);
        assertNull(store.previousManifest());
    }

    @Test
    public void verifiedStagingRotatesActiveToPrevious() throws Exception {
        HomecomingSnapshotStore store = new HomecomingSnapshotStore(folder.getRoot());
        writeVerifiedSnapshot(store, "active", "active-old");
        writeVerifiedSnapshot(store, "staging", "active-new");

        assertTrue(store.activateStaging());
        assertEquals("active-new", store.activeManifest().snapshotId);
        assertEquals("active-old", store.previousManifest().snapshotId);
    }

    @Test
    public void failedFinalRenameRestoresOriginalActive() throws Exception {
        HomecomingSnapshotStore.FileOperations operations =
                new HomecomingSnapshotStore.DefaultFileOperations() {
                    @Override
                    public boolean rename(File source, File target) {
                        if ("staging".equals(source.getName()) && "active".equals(target.getName())) {
                            return false;
                        }
                        return super.rename(source, target);
                    }
                };
        HomecomingSnapshotStore store =
                new HomecomingSnapshotStore(folder.getRoot(), operations);
        writeVerifiedSnapshot(store, "active", "active-old");
        writeVerifiedSnapshot(store, "staging", "active-new");

        assertFalse(store.activateStaging());
        assertEquals("active-old", store.activeManifest().snapshotId);
        assertNull(store.previousManifest());
    }

    @Test
    public void changedPayloadOrSectionHashFailsVerification() throws Exception {
        HomecomingSnapshotStore store = new HomecomingSnapshotStore(folder.getRoot());
        writeVerifiedSnapshot(store, "staging", "snapshot-one");
        Files.write(
                new File(new File(folder.getRoot(), "staging"), "snapshot.json.gz").toPath(),
                "changed".getBytes(StandardCharsets.UTF_8));

        assertFalse(store.verifyStaging());
        assertFalse(store.activateStaging());
    }

    @Test
    public void verificationPreservesPythonFloatLexemesWhenHashingSections() throws Exception {
        HomecomingSnapshotStore store = new HomecomingSnapshotStore(folder.getRoot());
        String identityJson = "{\"created_at\":1800000000.0,\"name\":\"Fixture\"}";
        String payloadJson = "{\"schema\":2,\"sections\":{\"identity\":"
                + identityJson + "},\"snapshot_id\":\"python-float\"}";
        byte[] compressed = HomecomingSnapshotStore.gzip(
                payloadJson.getBytes(StandardCharsets.UTF_8));
        JSONObject manifest = new JSONObject()
                .put("schema", 2)
                .put("snapshot_id", "python-float")
                .put("file_sha256", HomecomingSnapshotStore.sha256Hex(compressed))
                .put("section_hashes", new JSONObject().put(
                        "identity",
                        HomecomingSnapshotStore.sha256Hex(
                                identityJson.getBytes(StandardCharsets.UTF_8))));
        store.beginStaging("python-float");
        store.writeStagingFile("snapshot.json.gz", compressed);
        store.writeStagingFile(
                "manifest.json",
                HomecomingSnapshotStore.canonicalJson(manifest)
                        .getBytes(StandardCharsets.UTF_8));
        store.writeStagingFile("READY", new byte[0]);

        assertTrue(store.verifyStaging());
    }

    @Test
    public void utf8RangeHashMatchesOriginalBytesWithoutCopyingWholeMember() {
        String source = "prefix:{\"text\":\"鬣狗🐾\",\"value\":1800000000.0}:suffix";
        int start = source.indexOf('{');
        int end = source.lastIndexOf('}') + 1;

        assertEquals(
                HomecomingSnapshotStore.sha256Hex(
                        source.substring(start, end).getBytes(StandardCharsets.UTF_8)),
                HomecomingSnapshotStore.sha256HexUtf8(source, start, end));
    }

    @Test
    public void largeRawSectionStillVerifiesAndRejectsChangedHash() throws Exception {
        HomecomingSnapshotStore store = new HomecomingSnapshotStore(folder.getRoot());
        StringBuilder text = new StringBuilder(24 * 1024 * 1024);
        while (text.length() < 24 * 1024 * 1024) text.append("归巢-data-");
        String identityJson = "{\"created_at\":1800000000.0,\"text\":"
                + JSONObject.quote(text.toString()) + "}";
        writeRawSnapshot(store, "large-section", identityJson,
                HomecomingSnapshotStore.sha256Hex(
                        identityJson.getBytes(StandardCharsets.UTF_8)));

        assertTrue(store.verifyStaging());

        writeRawSnapshot(store, "large-section", identityJson,
                HomecomingSnapshotStore.sha256Hex("changed".getBytes(StandardCharsets.UTF_8)));
        assertFalse(store.verifyStaging());
    }

    private void writeRawSnapshot(
            HomecomingSnapshotStore store, String snapshotId,
            String identityJson, String identityHash) throws Exception {
        String payloadJson = "{\"schema\":2,\"sections\":{\"identity\":"
                + identityJson + "},\"snapshot_id\":" + JSONObject.quote(snapshotId) + "}";
        byte[] compressed = HomecomingSnapshotStore.gzip(
                payloadJson.getBytes(StandardCharsets.UTF_8));
        JSONObject manifest = new JSONObject()
                .put("schema", 2)
                .put("snapshot_id", snapshotId)
                .put("file_sha256", HomecomingSnapshotStore.sha256Hex(compressed))
                .put("section_hashes", new JSONObject().put("identity", identityHash));
        store.beginStaging(snapshotId);
        store.writeStagingFile("snapshot.json.gz", compressed);
        store.writeStagingFile("manifest.json",
                HomecomingSnapshotStore.canonicalJson(manifest)
                        .getBytes(StandardCharsets.UTF_8));
        store.writeStagingFile("READY", new byte[0]);
    }

    private void writeVerifiedSnapshot(
            HomecomingSnapshotStore store, String directoryName, String snapshotId)
            throws Exception {
        File directory = new File(folder.getRoot(), directoryName);
        assertTrue(directory.mkdirs() || directory.isDirectory());
        JSONObject identity = new JSONObject().put("configured_name", "Fixture");
        JSONObject sections = new JSONObject().put("identity", identity);
        JSONObject payload = new JSONObject()
                .put("schema", HomecomingContract.SCHEMA_VERSION)
                .put("snapshot_id", snapshotId)
                .put("sections", sections);
        byte[] compressed = HomecomingSnapshotStore.gzip(
                HomecomingSnapshotStore.canonicalJson(payload).getBytes(StandardCharsets.UTF_8));
        Files.write(new File(directory, "snapshot.json.gz").toPath(), compressed);

        JSONObject manifest = new JSONObject()
                .put("schema", HomecomingContract.SCHEMA_VERSION)
                .put("snapshot_id", snapshotId)
                .put("file_sha256", HomecomingSnapshotStore.sha256Hex(compressed))
                .put("section_hashes", new JSONObject(Collections.singletonMap(
                        "identity",
                        HomecomingSnapshotStore.sha256Hex(
                                HomecomingSnapshotStore.canonicalJson(identity)
                                        .getBytes(StandardCharsets.UTF_8)))));
        Files.write(
                new File(directory, "manifest.json").toPath(),
                HomecomingSnapshotStore.canonicalJson(manifest)
                        .getBytes(StandardCharsets.UTF_8));
        Files.write(new File(directory, "READY").toPath(), new byte[0]);
    }
}
