package com.aion.chat;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

public class WebViewLaunchPolicyTest {
    @Test
    public void addressPickerSelectionWins() {
        assertEquals(
                "http://selected/chat",
                WebViewLaunchPolicy.resolveUrl(
                        "http://selected/chat",
                        true,
                        "http://saved/chat"));
    }

    @Test
    public void desktopColdLaunchUsesRememberedAddress() {
        assertEquals(
                "http://saved/chat",
                WebViewLaunchPolicy.resolveUrl(
                        null,
                        true,
                        "http://saved/chat"));
    }

    @Test
    public void desktopColdLaunchWithoutRememberedChoiceShowsAddressPicker() {
        assertNull(WebViewLaunchPolicy.resolveUrl(
                null,
                false,
                "http://saved/chat"));
    }
}
