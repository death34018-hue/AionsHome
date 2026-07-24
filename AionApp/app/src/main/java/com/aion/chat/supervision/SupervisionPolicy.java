package com.aion.chat.supervision;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;

public final class SupervisionPolicy {
    private final long idleResetMs;
    private final List<Long> checkpointsMs;
    private final String roleId;

    private SupervisionPolicy(long idleResetMs, List<Long> checkpointsMs, String roleId) {
        this.idleResetMs = idleResetMs;
        this.checkpointsMs = checkpointsMs;
        this.roleId = roleId;
    }

    public static SupervisionPolicy of(
            long idleResetMs, List<Long> checkpointsMs, String roleId) {
        if (idleResetMs <= 0) {
            throw new IllegalArgumentException("idleResetMs must be positive");
        }
        if (checkpointsMs == null) {
            throw new IllegalArgumentException("checkpointsMs is required");
        }
        if (roleId == null || roleId.trim().isEmpty()) {
            throw new IllegalArgumentException("roleId is required");
        }
        ArrayList<Long> checked = new ArrayList<>();
        for (Long checkpoint : checkpointsMs) {
            if (checkpoint == null || checkpoint <= 0) {
                throw new IllegalArgumentException("checkpoints must be positive");
            }
            checked.add(checkpoint);
        }
        Collections.sort(checked);
        checked = new ArrayList<>(new LinkedHashSet<>(checked));
        return new SupervisionPolicy(
                idleResetMs,
                Collections.unmodifiableList(checked),
                roleId.trim());
    }

    public long getIdleResetMs() {
        return idleResetMs;
    }

    public List<Long> getCheckpointsMs() {
        return checkpointsMs;
    }

    public String getRoleId() {
        return roleId;
    }
}
