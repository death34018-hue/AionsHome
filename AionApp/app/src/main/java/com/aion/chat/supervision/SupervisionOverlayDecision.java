package com.aion.chat.supervision;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

public final class SupervisionOverlayDecision {
    public enum Mode {
        HIDDEN,
        DEVICE_LOCK,
        APP_LOCK
    }

    private static final Set<String> PHONE_SAFETY_PACKAGES = new HashSet<>(Arrays.asList(
            "com.android.systemui",
            "com.android.phone",
            "com.android.dialer",
            "com.google.android.dialer",
            "com.android.server.telecom",
            "com.android.incallui",
            "com.google.android.incallui",
            "com.android.emergency"));

    private SupervisionOverlayDecision() {}

    public static Mode decide(Input input) {
        if (input == null) throw new IllegalArgumentException("input is required");
        String foregroundPackage = normalize(input.foregroundPackage);
        if (input.aionsHomeForeground
                || foregroundPackage.equals(normalize(input.ownPackage))
                || isPhoneSafetyPackage(foregroundPackage)) {
            return Mode.HIDDEN;
        }
        if (input.deviceState == EffectiveState.LOCKED) {
            return Mode.DEVICE_LOCK;
        }
        if (input.appState == EffectiveState.LOCKED) {
            return Mode.APP_LOCK;
        }
        return Mode.HIDDEN;
    }

    public static boolean isPhoneSafetyPackage(String packageName) {
        String normalized = normalize(packageName);
        return PHONE_SAFETY_PACKAGES.contains(normalized)
                || normalized.contains("incallui")
                || normalized.contains("emergency");
    }

    private static String normalize(String value) {
        return value == null ? "" : value.trim().toLowerCase(Locale.US);
    }

    public static final class Input {
        private final String ownPackage;
        private final String foregroundPackage;
        private final boolean aionsHomeForeground;
        private final EffectiveState deviceState;
        private final EffectiveState appState;

        private Input(Builder builder) {
            ownPackage = builder.ownPackage;
            foregroundPackage = builder.foregroundPackage;
            aionsHomeForeground = builder.aionsHomeForeground;
            deviceState = builder.deviceState;
            appState = builder.appState;
        }

        public static Builder builder(String ownPackage) {
            return new Builder(ownPackage);
        }

        public static final class Builder {
            private final String ownPackage;
            private String foregroundPackage = "";
            private boolean aionsHomeForeground;
            private EffectiveState deviceState = EffectiveState.NORMAL;
            private EffectiveState appState = EffectiveState.NORMAL;

            private Builder(String ownPackage) {
                this.ownPackage = ownPackage == null ? "" : ownPackage;
            }

            public Builder foregroundPackage(String value) {
                foregroundPackage = value;
                return this;
            }

            public Builder aionsHomeForeground(boolean value) {
                aionsHomeForeground = value;
                return this;
            }

            public Builder deviceState(EffectiveState value) {
                deviceState = value == null ? EffectiveState.NORMAL : value;
                return this;
            }

            public Builder appState(EffectiveState value) {
                appState = value == null ? EffectiveState.NORMAL : value;
                return this;
            }

            public Input build() {
                return new Input(this);
            }
        }
    }
}
