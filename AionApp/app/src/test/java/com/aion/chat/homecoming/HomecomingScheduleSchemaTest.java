package com.aion.chat.homecoming;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

public class HomecomingScheduleSchemaTest {
    @Test
    public void scheduleOverlayAndExecutionLedgerRemainInCurrentSchema() {
        assertEquals(6, HomecomingDatabase.DATABASE_VERSION);
        assertTrue(HomecomingDatabase.CREATE_STATEMENTS.contains("schedule_local"));
        assertTrue(HomecomingDatabase.CREATE_STATEMENTS.contains("schedule_execution"));
        assertTrue(HomecomingDatabase.CREATE_STATEMENTS.contains(
                "UNIQUE(schedule_id, trigger_at)"));
        assertTrue(HomecomingDatabase.tableNames().contains("schedule_local"));
        assertTrue(HomecomingDatabase.tableNames().contains("schedule_execution"));
    }

    @Test
    public void scheduleWritesRemainOutsideSnapshotRefreshTables() {
        assertTrue(HomecomingDatabase.snapshotTableNames().contains("schedule_snapshot"));
        assertTrue(!HomecomingDatabase.snapshotTableNames().contains("schedule_local"));
        assertTrue(!HomecomingDatabase.snapshotTableNames().contains("schedule_execution"));
    }
}
