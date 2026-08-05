package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.HashSet;
import java.util.Set;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingDatabaseContractTest {
    @Test
    public void schemaUsesOnlyTheDedicatedDatabaseAndSnapshotTables() {
        assertEquals("homecoming.db", HomecomingDatabase.DATABASE_NAME);
        assertEquals(6, HomecomingDatabase.DATABASE_VERSION);

        String sql = HomecomingDatabase.CREATE_STATEMENTS;
        assertFalse(sql.contains("CREATE TABLE messages "));
        assertFalse(sql.contains("CREATE TABLE memories "));
        assertTrue(sql.contains("message_snapshot"));
        assertTrue(sql.contains("memory_snapshot"));
        assertTrue(sql.contains("chat_message"));
        assertTrue(sql.contains("chat_request"));
        assertTrue(sql.contains("memory_local"));
        assertTrue(sql.contains("summary_anchor"));
        assertTrue(sql.contains("epoch_id TEXT NOT NULL"));
        assertTrue(sql.contains("PRIMARY KEY(epoch_id, owner_id)"));
        assertTrue(sql.contains("idx_homecoming_chat_epoch_timeline_time"));
        assertTrue(sql.contains("operation_journal"));
        assertTrue(sql.contains("schedule_local"));
        assertTrue(sql.contains("schedule_execution"));
        assertTrue(sql.contains("supervision_event"));
    }

    @Test
    public void schemaDeclaresEveryPhaseOneTableExactlyOnce() {
        Set<String> expected = new HashSet<>();
        expected.add("snapshot_meta");
        expected.add("identity_snapshot");
        expected.add("timeline_snapshot");
        expected.add("message_snapshot");
        expected.add("memory_snapshot");
        expected.add("schedule_snapshot");
        expected.add("runtime_snapshot");
        expected.add("route_descriptor");
        expected.add("homecoming_epoch");
        expected.add("chat_message");
        expected.add("chat_request");
        expected.add("memory_local");
        expected.add("summary_anchor");
        expected.add("operation_journal");
        expected.add("schedule_local");
        expected.add("schedule_execution");
        expected.add("supervision_event");
        expected.add("return_package");
        expected.add("return_attempt");

        assertEquals(expected, new HashSet<>(HomecomingDatabase.tableNames()));
        assertEquals(expected.size(), HomecomingDatabase.tableNames().size());
    }

    @Test
    public void snapshotRefreshCannotClearWritableHomecomingTables() {
        assertFalse(HomecomingDatabase.snapshotTableNames().contains("chat_message"));
        assertFalse(HomecomingDatabase.snapshotTableNames().contains("memory_local"));
        assertFalse(HomecomingDatabase.snapshotTableNames().contains("operation_journal"));
        assertFalse(HomecomingDatabase.snapshotTableNames().contains("schedule_local"));
        assertFalse(HomecomingDatabase.snapshotTableNames().contains("schedule_execution"));
        assertFalse(HomecomingDatabase.snapshotTableNames().contains("supervision_event"));
        assertFalse(HomecomingDatabase.snapshotTableNames().contains("return_package"));
        assertFalse(HomecomingDatabase.snapshotTableNames().contains("return_attempt"));
        assertTrue(HomecomingDatabase.snapshotTableNames().contains("message_snapshot"));
        assertTrue(HomecomingDatabase.snapshotTableNames().contains("memory_snapshot"));
    }
}
