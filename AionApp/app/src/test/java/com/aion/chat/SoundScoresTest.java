package com.aion.chat;

import org.junit.Test;
import static org.junit.Assert.*;

public class SoundScoresTest {
    @Test public void ambientDistinguishesCoughAndHummingFromSpeechAndNoise() {
        float[] scores = new float[521];
        scores[42] = .8f;
        assertEquals("cough", SoundScores.ambientKind(scores));
        scores[42] = 0; scores[32] = .7f;
        assertEquals("humming", SoundScores.ambientKind(scores));
        scores[32] = 0; scores[0] = .8f;
        assertEquals("speech", SoundScores.ambientKind(scores));
        scores[500] = .95f;
        assertEquals("background", SoundScores.ambientKind(scores));
    }
    @Test public void backgroundSoundCanWinOverSpeechAndSnoring() {
        float[] scores = new float[521];
        scores[0] = 0.14f;
        scores[38] = 0.09f;
        scores[132] = 0.81f;
        int[] top = SoundScores.top(scores, 3);
        assertArrayEquals(new int[]{132, 0, 38}, top);
    }

    @Test public void relatedLabelsUseStrongestScoreWithoutAddingProbabilities() {
        float[] scores = new float[521];
        scores[0] = 0.45f;
        scores[2] = 0.70f;
        scores[12] = 0.30f;
        scores[24] = 0.10f;
        scores[29] = 0.65f;
        scores[32] = 0.82f;
        scores[38] = 0.03f;
        assertArrayEquals(new float[]{0.70f, 0.65f, 0.82f, 0.03f},
                SoundScores.focus(scores), 0.0001f);
    }
}
