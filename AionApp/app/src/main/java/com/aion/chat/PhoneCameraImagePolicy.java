package com.aion.chat;

import java.util.ArrayList;
import java.util.List;

final class PhoneCameraImagePolicy {
    static final int MAX_LONG_EDGE = 1280;
    static final int MAX_JPEG_BYTES = 800 * 1024;
    static final int INITIAL_JPEG_QUALITY = 80;
    static final int MIN_JPEG_QUALITY = 55;

    private PhoneCameraImagePolicy() {}

    static String normalizeFacing(String facing) {
        String value = facing == null ? "" : facing.trim().toLowerCase();
        return ("front".equals(value) || "user".equals(value)) ? "front" : "back";
    }

    static float clampZoom(float requested, float minimum, float maximum) {
        float safeMinimum = Math.max(0.1f, minimum);
        float safeMaximum = Math.max(safeMinimum, maximum);
        if (Float.isNaN(requested) || Float.isInfinite(requested)) requested = 1f;
        return Math.max(safeMinimum, Math.min(safeMaximum, requested));
    }

    static List<Float> zoomPresets(float minimum, float maximum) {
        float min = Math.max(0.1f, minimum);
        float max = Math.max(min, maximum);
        float[] candidates = new float[]{min, 0.8f, 1f, 2f, 3.5f, 5f, max};
        List<Float> result = new ArrayList<>();
        for (float value : candidates) {
            if (value < min - 0.0001f || value > max + 0.0001f) continue;
            boolean duplicate = false;
            for (float existing : result) {
                if (Math.abs(existing - value) < 0.0001f) {
                    duplicate = true;
                    break;
                }
            }
            if (!duplicate) result.add(value);
        }
        return result;
    }

    static int[] targetDimensions(int width, int height, int maxLongEdge) {
        if (width <= 0 || height <= 0) return new int[]{0, 0};
        int longest = Math.max(width, height);
        int target = Math.max(1, maxLongEdge);
        if (longest <= target) return new int[]{width, height};
        float scale = target / (float) longest;
        return new int[]{
                Math.max(1, Math.round(width * scale)),
                Math.max(1, Math.round(height * scale))
        };
    }

    static int nextJpegQuality(int current) {
        if (current <= MIN_JPEG_QUALITY) return MIN_JPEG_QUALITY;
        return Math.max(MIN_JPEG_QUALITY, current - 5);
    }
}
