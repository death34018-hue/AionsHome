package com.aion.chat;

import org.junit.Test;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class LauncherResumePolicyTest {
    @Test
    public void repeatedExternalLaunchReusesTheExistingWebViewTask() {
        assertTrue(LauncherResumePolicy.shouldFinishWithoutLaunching(
                false,
                false));
    }

    @Test
    public void coldLaunchStillCreatesTheFirstWebView() {
        assertFalse(LauncherResumePolicy.shouldFinishWithoutLaunching(
                true,
                false));
    }

    @Test
    public void explicitAddressSwitchStillShowsTheLauncher() {
        assertFalse(LauncherResumePolicy.shouldFinishWithoutLaunching(
                false,
                true));
    }
}
