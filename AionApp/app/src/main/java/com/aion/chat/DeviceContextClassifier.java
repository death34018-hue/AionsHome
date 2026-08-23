package com.aion.chat;

import org.json.JSONObject;
import org.json.JSONException;

/** Pure state classifier. It never owns Android sensors or networking. */
public final class DeviceContextClassifier {
    private static final long STABLE_MS = 2_000L;
    private static final long HEARTBEAT_MS = 5 * 60_000L;

    private String posture = "unknown";
    private String postureCandidate = "";
    private long postureCandidateAtMs;
    private long postureSinceMs;
    private long postureObservedAtMs;

    private String motion = "unknown";
    private String motionCandidate = "";
    private long motionCandidateAtMs;
    private long motionSinceMs;
    private long motionObservedAtMs;

    private String light = "unknown";
    private long lightSinceMs;
    private long lightObservedAtMs;
    private String proximity = "unknown";
    private long proximitySinceMs;
    private long proximityObservedAtMs;
    private String screen = "unknown";
    private long screenSinceMs;
    private long screenObservedAtMs;
    private String foregroundApp = "";
    private long foregroundAppSinceMs;
    private long foregroundAppObservedAtMs;

    private boolean dirty;
    private long lastEmittedAtMs;

    public synchronized void updateGravity(float x, float y, float z, long nowMs) {
        String next;
        float ax = Math.abs(x), ay = Math.abs(y), az = Math.abs(z);
        if (az >= 7f) {
            next = z < 0 ? "face_down" : "face_up";
        } else if (ax >= 5f && ax > ay) {
            next = x > 0 ? "landscape_left" : "landscape_right";
        } else if (ay >= 5f) {
            next = y > 0 ? "portrait" : "portrait_upside_down";
        } else {
            next = "tilted";
        }
        postureObservedAtMs = nowMs;
        if (next.equals(posture)) return;
        if (!next.equals(postureCandidate)) {
            postureCandidate = next;
            postureCandidateAtMs = nowMs;
            return;
        }
        if (nowMs - postureCandidateAtMs >= STABLE_MS) {
            posture = next;
            postureSinceMs = nowMs;
            dirty = true;
        }
    }

    public synchronized void updateMotion(float magnitude, long nowMs) {
        String next = magnitude < 0.18f ? "still"
                : magnitude < 1.2f ? "slight"
                : magnitude < 3.0f ? "moving" : "strong";
        motionObservedAtMs = nowMs;
        if (next.equals(motion)) return;
        if (!next.equals(motionCandidate)) {
            motionCandidate = next;
            motionCandidateAtMs = nowMs;
            return;
        }
        if (nowMs - motionCandidateAtMs >= STABLE_MS) {
            motion = next;
            motionSinceMs = nowMs;
            dirty = true;
        }
    }

    public synchronized void updateLight(float lux, long nowMs) {
        String next = lux < 8f ? "dark" : lux < 80f ? "dim" : lux < 350f ? "normal" : "bright";
        lightObservedAtMs = nowMs;
        if (!next.equals(light)) {
            light = next;
            lightSinceMs = nowMs;
            dirty = true;
        }
    }

    public synchronized void updateProximity(float distance, float maximumRange, long nowMs) {
        String next = distance < Math.min(5f, maximumRange) ? "near" : "far";
        proximityObservedAtMs = nowMs;
        if (!next.equals(proximity)) {
            proximity = next;
            proximitySinceMs = nowMs;
            dirty = true;
        }
    }

    public synchronized void updateScreen(boolean on, long nowMs) {
        String next = on ? "on" : "off";
        screenObservedAtMs = nowMs;
        if (!next.equals(screen)) {
            screen = next;
            screenSinceMs = nowMs;
            dirty = true;
        }
    }

    public synchronized void updateForegroundApp(String packageName, long nowMs) {
        String next = packageName == null ? "" : packageName.trim();
        if (next.isEmpty()) return;
        foregroundAppObservedAtMs = nowMs;
        if (!next.equals(foregroundApp)) {
            foregroundApp = next;
            foregroundAppSinceMs = nowMs;
            dirty = true;
        }
    }

    public synchronized JSONObject snapshot(long nowMs) {
        JSONObject result = new JSONObject();
        putSlot(result, "posture", posture, postureObservedAtMs, postureSinceMs, 0.9);
        putSlot(result, "motion", motion, motionObservedAtMs, motionSinceMs, 0.8);
        putSlot(result, "light", light, lightObservedAtMs, lightSinceMs, 0.95);
        putSlot(result, "proximity", proximity, proximityObservedAtMs, proximitySinceMs, 0.95);
        putSlot(result, "screen", screen, screenObservedAtMs, screenSinceMs, 1.0);
        putSlot(result, "foreground_app", foregroundApp, foregroundAppObservedAtMs,
                foregroundAppSinceMs, 0.85);
        return result;
    }

    private static void putSlot(JSONObject target, String key, String value,
                                long observedAtMs, long sinceMs, double confidence) {
        if (value == null || value.isEmpty() || "unknown".equals(value) || observedAtMs <= 0) return;
        try {
            JSONObject slot = new JSONObject();
            slot.put("value", value);
            slot.put("observed_at", observedAtMs / 1000.0);
            slot.put("since", sinceMs / 1000.0);
            slot.put("confidence", confidence);
            target.put(key, slot);
        } catch (JSONException ignored) {
            // Primitive values above are JSON-safe; Android keeps the checked signature.
        }
    }

    public synchronized boolean shouldEmit(long nowMs) {
        return dirty || nowMs - lastEmittedAtMs >= HEARTBEAT_MS;
    }

    public synchronized void markEmitted(long nowMs) {
        dirty = false;
        lastEmittedAtMs = nowMs;
    }

    public synchronized String getPosture() { return posture; }
    public synchronized long getPostureSinceMs() { return postureSinceMs; }
    public synchronized String getLightLevel() { return light; }
}
