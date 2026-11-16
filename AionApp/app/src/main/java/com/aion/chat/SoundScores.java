package com.aion.chat;

import java.util.Arrays;

/** YAMNet is multi-label: keep background winners and never sum overlapping labels. */
final class SoundScores {
    static String ambientKind(float[] scores) {
        int winner = top(scores, 1)[0];
        if (scores[winner] < .25f) return "background";
        if (winner == 42) return "cough";
        if (winner == 32) return "humming";
        if (winner == 38) return "snoring";
        if (winner >= 24 && winner <= 31) return "singing";
        if (winner <= 3 || winner == 12) return "speech";
        return "background";
    }

    static int[] top(float[] scores, int count) {
        Integer[] indices = new Integer[scores.length];
        for (int i = 0; i < indices.length; i++) indices[i] = i;
        Arrays.sort(indices, (a, b) -> Float.compare(scores[b], scores[a]));
        int[] result = new int[Math.min(count, scores.length)];
        for (int i = 0; i < result.length; i++) result[i] = indices[i];
        return result;
    }

    static float[] focus(float[] scores) {
        return new float[]{
                max(scores, 0, 1, 2, 3, 12),
                max(scores, 24, 25, 26, 27, 28, 29, 30, 31),
                scores[32], scores[38]
        };
    }

    private static float max(float[] scores, int... indices) {
        float best = 0;
        for (int index : indices) best = Math.max(best, scores[index]);
        return best;
    }
}
