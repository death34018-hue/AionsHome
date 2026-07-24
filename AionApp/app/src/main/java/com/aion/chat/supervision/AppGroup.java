package com.aion.chat.supervision;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;

public final class AppGroup {
    private final String groupId;
    private final String displayName;
    private final List<String> packageNames;
    private final boolean monitored;
    private final SupervisionPolicy policy;

    private AppGroup(String groupId, String displayName, List<String> packageNames,
            boolean monitored, SupervisionPolicy policy) {
        this.groupId = groupId;
        this.displayName = displayName;
        this.packageNames = packageNames;
        this.monitored = monitored;
        this.policy = policy;
    }

    public static AppGroup create(String groupId, String displayName, List<String> packageNames,
            boolean monitored, SupervisionPolicy policy) {
        if (groupId == null || groupId.trim().isEmpty()) {
            throw new IllegalArgumentException("groupId is required");
        }
        if (displayName == null || displayName.trim().isEmpty()) {
            throw new IllegalArgumentException("displayName is required");
        }
        if (packageNames == null || packageNames.isEmpty()) {
            throw new IllegalArgumentException("at least one package is required");
        }
        if (policy == null) {
            throw new IllegalArgumentException("policy is required");
        }
        LinkedHashSet<String> checked = new LinkedHashSet<>();
        for (String packageName : packageNames) {
            if (packageName == null || packageName.trim().isEmpty()) {
                throw new IllegalArgumentException("package name is required");
            }
            checked.add(packageName.trim());
        }
        return new AppGroup(
                groupId.trim(),
                displayName.trim(),
                Collections.unmodifiableList(new ArrayList<>(checked)),
                monitored,
                policy);
    }

    public String getGroupId() {
        return groupId;
    }

    public String getDisplayName() {
        return displayName;
    }

    public List<String> getPackageNames() {
        return packageNames;
    }

    public boolean isMonitored() {
        return monitored;
    }

    public SupervisionPolicy getPolicy() {
        return policy;
    }
}
