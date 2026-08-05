package com.aion.chat;

import org.junit.Test;

import java.util.Arrays;
import java.util.List;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;

public class PhoneCameraImagePolicyTest {

    @Test
    public void clampsZoomToReportedRange() {
        assertEquals(0.6f, PhoneCameraImagePolicy.clampZoom(0.2f, 0.6f, 10f), 0.0001f);
        assertEquals(2.0f, PhoneCameraImagePolicy.clampZoom(2.0f, 0.6f, 10f), 0.0001f);
        assertEquals(10f, PhoneCameraImagePolicy.clampZoom(20f, 0.6f, 10f), 0.0001f);
    }

    @Test
    public void createsUsefulPresetRatiosInsideRange() {
        List<Float> back = PhoneCameraImagePolicy.zoomPresets(0.6f, 10f);
        assertEquals(
                Arrays.asList(0.6f, 0.8f, 1f, 2f, 3.5f, 5f, 10f),
                back
        );
        List<Float> front = PhoneCameraImagePolicy.zoomPresets(1f, 4f);
        assertEquals(Arrays.asList(1f, 2f, 3.5f, 4f), front);
    }

    @Test
    public void calculatesTargetSizeWithLongestEdge1280() {
        assertArrayEquals(
                new int[]{1280, 960},
                PhoneCameraImagePolicy.targetDimensions(4000, 3000, 1280)
        );
        assertArrayEquals(
                new int[]{720, 1280},
                PhoneCameraImagePolicy.targetDimensions(1080, 1920, 1280)
        );
        assertArrayEquals(
                new int[]{640, 480},
                PhoneCameraImagePolicy.targetDimensions(640, 480, 1280)
        );
    }

    @Test
    public void lowersJpegQualityWithoutDroppingBelowFloor() {
        assertEquals(75, PhoneCameraImagePolicy.nextJpegQuality(80));
        assertEquals(55, PhoneCameraImagePolicy.nextJpegQuality(56));
        assertEquals(55, PhoneCameraImagePolicy.nextJpegQuality(55));
    }

    @Test
    public void normalizesFacingNames() {
        assertEquals("front", PhoneCameraImagePolicy.normalizeFacing("user"));
        assertEquals("front", PhoneCameraImagePolicy.normalizeFacing("FRONT"));
        assertEquals("back", PhoneCameraImagePolicy.normalizeFacing("environment"));
        assertEquals("back", PhoneCameraImagePolicy.normalizeFacing("unexpected"));
    }
}
