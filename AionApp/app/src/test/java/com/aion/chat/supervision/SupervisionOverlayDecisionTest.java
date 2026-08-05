package com.aion.chat.supervision;

import org.junit.Test;

import static com.aion.chat.supervision.SupervisionOverlayDecision.Mode.APP_LOCK;
import static com.aion.chat.supervision.SupervisionOverlayDecision.Mode.DEVICE_LOCK;
import static com.aion.chat.supervision.SupervisionOverlayDecision.Mode.HIDDEN;
import static org.junit.Assert.assertEquals;

public class SupervisionOverlayDecisionTest {
    @Test
    public void ownAppAlwaysHidesDeviceOverlay() {
        assertEquals(HIDDEN, SupervisionOverlayDecision.decide(input()
                .deviceState(EffectiveState.LOCKED)
                .aionsHomeForeground(true)
                .foregroundPackage("com.example.video")
                .build()));
        assertEquals(HIDDEN, SupervisionOverlayDecision.decide(input()
                .deviceState(EffectiveState.LOCKED)
                .foregroundPackage("com.aion.chat")
                .build()));
    }

    @Test
    public void deviceLockWinsOverAppLockForOrdinaryApp() {
        assertEquals(DEVICE_LOCK, SupervisionOverlayDecision.decide(input()
                .deviceState(EffectiveState.LOCKED)
                .appState(EffectiveState.LOCKED)
                .foregroundPackage("com.xingin.xhs")
                .build()));
    }

    @Test
    public void deviceTemporaryUnlockFallsBackToIndependentAppLock() {
        assertEquals(APP_LOCK, SupervisionOverlayDecision.decide(input()
                .deviceState(EffectiveState.TEMPORARILY_UNLOCKED)
                .appState(EffectiveState.LOCKED)
                .foregroundPackage("com.xingin.xhs")
                .build()));
    }

    @Test
    public void phoneSafetyAndSystemUiPackagesHideDeviceOverlay() {
        String[] packages = {
                "com.android.systemui",
                "com.android.phone",
                "com.android.dialer",
                "com.google.android.dialer",
                "com.android.server.telecom",
                "com.android.incallui"
        };
        for (String packageName : packages) {
            assertEquals(packageName, HIDDEN,
                    SupervisionOverlayDecision.decide(input()
                            .deviceState(EffectiveState.LOCKED)
                            .foregroundPackage(packageName)
                            .build()));
        }
    }

    @Test
    public void ordinaryUnlockedAppHasNoOverlay() {
        assertEquals(HIDDEN, SupervisionOverlayDecision.decide(input()
                .foregroundPackage("com.example.notes")
                .build()));
    }

    private static SupervisionOverlayDecision.Input.Builder input() {
        return SupervisionOverlayDecision.Input.builder("com.aion.chat");
    }
}
