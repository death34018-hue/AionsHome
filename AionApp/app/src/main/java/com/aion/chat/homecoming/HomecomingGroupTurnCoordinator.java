package com.aion.chat.homecoming;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

final class HomecomingGroupTurnCoordinator {
    private final List<String> owners;
    private final ReplyPort replies;
    private final Set<String> stopped = Collections.synchronizedSet(new HashSet<>());

    HomecomingGroupTurnCoordinator(List<String> owners, ReplyPort replies) {
        if (owners == null || owners.size() != 2) {
            throw new IllegalArgumentException("two group owners are required");
        }
        this.owners = Collections.unmodifiableList(new ArrayList<>(owners));
        this.replies = replies;
    }

    void start(String parentRequestId, Observer observer) {
        stopped.remove(parentRequestId);
        startOwner(parentRequestId, observer, 0);
    }

    void stop(String parentRequestId) {
        stopped.add(parentRequestId);
        replies.cancel(parentRequestId);
    }

    private void startOwner(
            String parentRequestId, Observer observer, int index) {
        if (stopped.contains(parentRequestId)) return;
        if (index >= owners.size()) {
            observer.onTurnComplete();
            return;
        }
        String ownerId = owners.get(index);
        String childRequestId = index == 0
                ? parentRequestId : parentRequestId + ":" + ownerId;
        replies.start(
                parentRequestId,
                childRequestId,
                ownerId,
                index == 0,
                new ReplyCompletion() {
                    @Override public void onComplete(String messageId, String text) {
                        if (stopped.contains(parentRequestId)) return;
                        observer.onReplyComplete(ownerId, messageId, text);
                        startOwner(parentRequestId, observer, index + 1);
                    }

                    @Override public void onFailure(String code) {
                        if (stopped.contains(parentRequestId)) return;
                        observer.onReplyFailure(ownerId, code);
                        startOwner(parentRequestId, observer, index + 1);
                    }
                });
    }

    interface ReplyPort {
        void start(
                String parentRequestId,
                String childRequestId,
                String ownerId,
                boolean commitUser,
                ReplyCompletion completion);
        void cancel(String parentRequestId);
    }

    interface ReplyCompletion {
        void onComplete(String messageId, String text);
        void onFailure(String code);
    }

    interface Observer {
        void onReplyComplete(String ownerId, String messageId, String text);
        void onReplyFailure(String ownerId, String code);
        void onTurnComplete();
    }
}
