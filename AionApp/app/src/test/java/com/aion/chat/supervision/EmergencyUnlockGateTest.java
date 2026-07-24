package com.aion.chat.supervision;

import org.junit.Test;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class EmergencyUnlockGateTest {
    @Test
    public void requiresExactTargetAndUninterruptedEightSecondHold() {
        Harness harness = new Harness("group-a", "group-b");

        EmergencyUnlockGate.Snapshot wrong = harness.gate.begin(
                "group-a", "group-b", 0L);
        assertEquals(EmergencyUnlockGate.Phase.CANCELLED, wrong.getPhase());

        EmergencyUnlockGate.Snapshot started = harness.gate.begin(
                "group-a", "group-a", 1_000L);
        assertEquals(EmergencyUnlockGate.Phase.HOLDING, started.getPhase());
        assertEquals(4_000L, harness.gate.holdProgress(
                "group-a", "group-a", true, 5_000L).getHeldMs());

        harness.gate.holdProgress("group-a", "group-a", false, 6_000L);
        assertEquals(EmergencyUnlockGate.Phase.CANCELLED,
                harness.gate.snapshot(6_000L).getPhase());

        harness.gate.begin("group-a", "group-a", 10_000L);
        EmergencyUnlockGate.Snapshot held = harness.gate.holdProgress(
                "group-a", "group-a", true, 18_000L);
        assertEquals(EmergencyUnlockGate.Phase.REASON_REQUIRED, held.getPhase());
    }

    @Test
    public void nonblankReasonStartsSixtySecondDelayBeforeConfirm() {
        Harness harness = new Harness("group-a");
        harness.completeHold("group-a", 0L);

        EmergencyUnlockGate.Snapshot blank = harness.gate.submitReason(
                "group-a", "group-a", "   ", 8_000L);
        assertEquals(EmergencyUnlockGate.Phase.REASON_REQUIRED, blank.getPhase());
        assertTrue(blank.getError().contains("reason"));

        EmergencyUnlockGate.Snapshot waiting = harness.gate.submitReason(
                "group-a", "group-a", "需要处理紧急事务", 9_000L);
        assertEquals(EmergencyUnlockGate.Phase.WAITING, waiting.getPhase());
        assertEquals(60_000L, harness.gate.remainingDelay(9_000L));
        assertFalse(harness.gate.confirm("group-a", "group-a", 68_999L).isOk());
        assertTrue(harness.locks.contains("group-a"));

        EmergencyUnlockGate.Result result = harness.gate.confirm(
                "group-a", "group-a", 69_000L);
        assertTrue(result.isOk());
        assertFalse(harness.locks.contains("group-a"));
        assertEquals("completed:group-a:需要处理紧急事务",
                harness.audit.get(harness.audit.size() - 1));
    }

    @Test
    public void targetChangeCancelsAndConfirmRemovesOnlySelectedLock() {
        Harness harness = new Harness("group-a", "group-b");
        harness.gate.begin("group-a", "group-a", 0L);

        EmergencyUnlockGate.Snapshot changed = harness.gate.holdProgress(
                "group-a", "group-b", true, 4_000L);

        assertEquals(EmergencyUnlockGate.Phase.CANCELLED, changed.getPhase());
        assertTrue(harness.audit.get(harness.audit.size() - 1).startsWith("cancelled:group-a"));

        harness.completeHold("group-a", 10_000L);
        harness.gate.submitReason("group-a", "group-a", "只解除这一项", 18_000L);
        assertTrue(harness.gate.confirm("group-a", "group-a", 78_000L).isOk());
        assertFalse(harness.locks.contains("group-a"));
        assertTrue(harness.locks.contains("group-b"));
    }

    private static final class Harness {
        final Set<String> locks = new LinkedHashSet<>();
        final List<String> audit = new ArrayList<>();
        final EmergencyUnlockGate gate;

        Harness(String... groupIds) {
            java.util.Collections.addAll(locks, groupIds);
            gate = new EmergencyUnlockGate(
                    new EmergencyUnlockGate.LockController() {
                        @Override public boolean isLocked(String groupId) {
                            return locks.contains(groupId);
                        }
                        @Override public void removeLock(String groupId) {
                            locks.remove(groupId);
                        }
                    },
                    (event, groupId, reason) -> audit.add(
                            event + ":" + groupId + ":" + reason));
        }

        void completeHold(String groupId, long startMs) {
            gate.begin(groupId, groupId, startMs);
            gate.holdProgress(groupId, groupId, true, startMs + 8_000L);
        }
    }
}
