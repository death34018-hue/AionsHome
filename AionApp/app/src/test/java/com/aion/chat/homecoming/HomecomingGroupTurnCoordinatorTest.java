package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingGroupTurnCoordinatorTest {
    @Test
    public void runsBothOwnersInOrderAndCommitsUserOnlyForTheFirst() {
        RecordingPort port = new RecordingPort();
        RecordingObserver observer = new RecordingObserver();
        HomecomingGroupTurnCoordinator coordinator =
                new HomecomingGroupTurnCoordinator(
                        Arrays.asList("second", "main"), port);

        coordinator.start("turn-one", observer);

        assertEquals(Arrays.asList(
                "turn-one:second:true",
                "turn-one:main:main:false"), port.starts);
        assertEquals(Arrays.asList("second", "main"), observer.completedOwners);
        assertTrue(observer.complete);
    }

    @Test
    public void firstFailureDoesNotPreventTheSecondOwner() {
        RecordingPort port = new RecordingPort();
        port.failOwner = "second";
        RecordingObserver observer = new RecordingObserver();
        HomecomingGroupTurnCoordinator coordinator =
                new HomecomingGroupTurnCoordinator(
                        Arrays.asList("second", "main"), port);

        coordinator.start("turn-two", observer);

        assertEquals(2, port.starts.size());
        assertEquals(Arrays.asList("second"), observer.failedOwners);
        assertEquals(Arrays.asList("main"), observer.completedOwners);
        assertTrue(observer.complete);
    }

    @Test
    public void stopCancelsCurrentReplyAndPreventsTheNextOwner() {
        RecordingPort port = new RecordingPort();
        port.defer = true;
        RecordingObserver observer = new RecordingObserver();
        HomecomingGroupTurnCoordinator coordinator =
                new HomecomingGroupTurnCoordinator(
                        Arrays.asList("second", "main"), port);

        coordinator.start("turn-three", observer);
        coordinator.stop("turn-three");
        port.finishDeferred();

        assertEquals(Arrays.asList("turn-three:second:true"), port.starts);
        assertEquals("turn-three", port.cancelledParent);
        assertFalse(observer.complete);
    }

    private static final class RecordingPort
            implements HomecomingGroupTurnCoordinator.ReplyPort {
        final List<String> starts = new ArrayList<>();
        String failOwner = "";
        String cancelledParent = "";
        boolean defer;
        HomecomingGroupTurnCoordinator.ReplyCompletion deferred;
        String deferredOwner;

        @Override public void start(
                String parentRequestId, String childRequestId,
                String ownerId, boolean commitUser,
                HomecomingGroupTurnCoordinator.ReplyCompletion completion) {
            starts.add(childRequestId + ":" + ownerId + ":" + commitUser);
            if (defer) {
                deferred = completion;
                deferredOwner = ownerId;
            } else if (ownerId.equals(failOwner)) {
                completion.onFailure("failed");
            } else {
                completion.onComplete(ownerId + "-message", ownerId + "-reply");
            }
        }

        @Override public void cancel(String parentRequestId) {
            cancelledParent = parentRequestId;
        }

        void finishDeferred() {
            deferred.onComplete(deferredOwner + "-message", deferredOwner + "-reply");
        }
    }

    private static final class RecordingObserver
            implements HomecomingGroupTurnCoordinator.Observer {
        final List<String> completedOwners = new ArrayList<>();
        final List<String> failedOwners = new ArrayList<>();
        boolean complete;

        @Override public void onReplyComplete(
                String ownerId, String messageId, String text) {
            completedOwners.add(ownerId);
        }

        @Override public void onReplyFailure(String ownerId, String code) {
            failedOwners.add(ownerId);
        }

        @Override public void onTurnComplete() {
            complete = true;
        }
    }
}
