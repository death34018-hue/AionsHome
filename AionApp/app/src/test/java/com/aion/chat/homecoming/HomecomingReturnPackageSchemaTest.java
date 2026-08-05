package com.aion.chat.homecoming;

import org.junit.Test;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingReturnPackageSchemaTest {
    @Test
    public void versionFiveAddsReturnPackagesOutsideSnapshotRefresh() {
        assertEquals(6, HomecomingDatabase.DATABASE_VERSION);
        assertTrue(HomecomingDatabase.CREATE_STATEMENTS.contains(
                "CREATE TABLE return_package"));
        assertTrue(HomecomingDatabase.CREATE_STATEMENTS.contains(
                "CREATE TABLE return_attempt"));
        assertTrue(HomecomingDatabase.tableNames().contains("return_package"));
        assertTrue(HomecomingDatabase.tableNames().contains("return_attempt"));
        assertFalse(HomecomingDatabase.snapshotTableNames().contains(
                "return_package"));
        assertFalse(HomecomingDatabase.snapshotTableNames().contains(
                "return_attempt"));
    }

    @Test
    public void returnCoordinatorReadsBaseSnapshotIdFromImportedMetadata()
            throws Exception {
        String source = new String(
                Files.readAllBytes(Paths.get(
                        "src/main/java/com/aion/chat/homecoming/"
                                + "HomecomingReturnPackageCoordinator.java")),
                StandardCharsets.UTF_8);

        assertTrue(source.contains(
                "FROM snapshot_meta WHERE key='snapshot_id'"));
        assertFalse(source.contains("activeManifest()"));
    }
}
