package com.aion.chat;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class DeviceContextClassifierTest {
    @Test public void faceDownRequiresStableGravityBeforeCommit() {
        DeviceContextClassifier classifier = new DeviceContextClassifier();
        classifier.updateGravity(0f, 0f, -9.8f, 0L);
        assertNotEquals("face_down", classifier.getPosture());
        classifier.updateGravity(0f, 0f, -9.8f, 2_100L);
        assertEquals("face_down", classifier.getPosture());
    }

    @Test public void repeatedValueKeepsSinceTimestamp() {
        DeviceContextClassifier classifier = new DeviceContextClassifier();
        classifier.updateGravity(0f, 0f, 9.8f, 0L);
        classifier.updateGravity(0f, 0f, 9.8f, 2_100L);
        long since = classifier.getPostureSinceMs();
        classifier.updateGravity(0f, 0f, 9.8f, 4_200L);
        assertEquals(since, classifier.getPostureSinceMs());
    }

    @Test public void lightBucketsRemainDeterministic() {
        DeviceContextClassifier classifier = new DeviceContextClassifier();
        classifier.updateLight(3f, 1L);
        assertEquals("dark", classifier.getLightLevel());
        classifier.updateLight(500f, 2L);
        assertEquals("bright", classifier.getLightLevel());
    }

    @Test public void unchangedSnapshotNeedsHeartbeatOnlyAfterFiveMinutes() {
        DeviceContextClassifier classifier = new DeviceContextClassifier();
        classifier.markEmitted(0L);
        assertFalse(classifier.shouldEmit(299_999L));
        assertTrue(classifier.shouldEmit(300_000L));
    }
}
