package com.aion.chat.supervision;

import java.util.Collections;
import java.util.List;

public final class ForegroundWindowResolver {
    public enum Kind {
        APPLICATION,
        SYSTEM,
        INPUT_METHOD,
        ACCESSIBILITY_OVERLAY,
        OTHER
    }

    public static final class WindowFact {
        private final String packageName;
        private final Kind kind;
        private final boolean focused;
        private final boolean active;
        private final int layer;

        private WindowFact(String packageName, Kind kind,
                boolean focused, boolean active, int layer) {
            this.packageName = packageName == null ? "" : packageName.trim();
            this.kind = kind == null ? Kind.OTHER : kind;
            this.focused = focused;
            this.active = active;
            this.layer = layer;
        }

        public static WindowFact application(String packageName,
                boolean focused, boolean active, int layer) {
            return new WindowFact(packageName, Kind.APPLICATION,
                    focused, active, layer);
        }

        public static WindowFact system(String packageName,
                boolean focused, int layer) {
            return new WindowFact(packageName, Kind.SYSTEM,
                    focused, false, layer);
        }

        public static WindowFact inputMethod(String packageName,
                boolean focused, int layer) {
            return new WindowFact(packageName, Kind.INPUT_METHOD,
                    focused, false, layer);
        }

        public static WindowFact of(String packageName, Kind kind,
                boolean focused, boolean active, int layer) {
            return new WindowFact(packageName, kind, focused, active, layer);
        }
    }

    public String resolve(List<WindowFact> windows) {
        List<WindowFact> safeWindows = windows == null
                ? Collections.emptyList() : windows;
        WindowFact selected = null;
        for (WindowFact candidate : safeWindows) {
            if (candidate == null || candidate.kind != Kind.APPLICATION
                    || candidate.packageName.isEmpty()) {
                continue;
            }
            if (selected == null || compare(candidate, selected) > 0) {
                selected = candidate;
            }
        }
        return selected == null ? "" : selected.packageName;
    }

    private static int compare(WindowFact left, WindowFact right) {
        if (left.focused != right.focused) return left.focused ? 1 : -1;
        if (left.active != right.active) return left.active ? 1 : -1;
        return Integer.compare(left.layer, right.layer);
    }
}
