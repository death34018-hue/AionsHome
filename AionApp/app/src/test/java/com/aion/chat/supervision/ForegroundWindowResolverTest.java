package com.aion.chat.supervision;

import org.junit.Test;

import java.util.Arrays;

import static org.junit.Assert.assertEquals;

public class ForegroundWindowResolverTest {
    @Test
    public void systemOverlaysDoNotReplaceTheFocusedApplicationWindow() {
        ForegroundWindowResolver resolver = new ForegroundWindowResolver();

        String resolved = resolver.resolve(Arrays.asList(
                ForegroundWindowResolver.WindowFact.application(
                        "com.xingin.xhs", true, true, 10),
                ForegroundWindowResolver.WindowFact.system(
                        "com.vivo.systemuiplugin", true, 30),
                ForegroundWindowResolver.WindowFact.inputMethod(
                        "com.example.ime", true, 40)));

        assertEquals("com.xingin.xhs", resolved);
    }

    @Test
    public void aRealFocusedApplicationSwitchWinsOverThePreviousApp() {
        ForegroundWindowResolver resolver = new ForegroundWindowResolver();

        String resolved = resolver.resolve(Arrays.asList(
                ForegroundWindowResolver.WindowFact.application(
                        "com.xingin.xhs", false, false, 10),
                ForegroundWindowResolver.WindowFact.application(
                        "com.bbk.launcher2", true, true, 20),
                ForegroundWindowResolver.WindowFact.system(
                        "com.android.systemui", true, 30)));

        assertEquals("com.bbk.launcher2", resolved);
    }
}
