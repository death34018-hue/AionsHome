package com.aion.chat.homecoming;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingSupervisionSchemaTest {
    @Test
    public void versionFourAddsIsolatedSupervisionEventLedger() {
        assertEquals(6, HomecomingDatabase.DATABASE_VERSION);
        assertTrue(HomecomingDatabase.CREATE_STATEMENTS.contains(
                "CREATE TABLE supervision_event"));
        assertTrue(HomecomingDatabase.CREATE_STATEMENTS.contains(
                "event_id TEXT PRIMARY KEY"));
        assertTrue(HomecomingDatabase.tableNames().contains("supervision_event"));
    }

    @Test
    public void supervisionEventsAreNeverReplacedBySnapshotRefresh() {
        assertFalse(HomecomingDatabase.snapshotTableNames().contains(
                "supervision_event"));
    }
}
