package com.aion.chat;

import org.junit.After;
import org.junit.Test;
import static org.junit.Assert.*;

public class FloatingChatVisibilityTest {
    @After public void reset() {
        FloatingChatService.setAppForeground(true);
        FloatingChatService.setVisiblePage("/chat");
    }

    @Test public void readingAndTheaterAllowPreviewsInsideTheApp() {
        FloatingChatService.setAppForeground(true);
        for (String page : new String[]{"/", "/reading", "/theater"}) {
            FloatingChatService.setVisiblePage(page);
            assertFalse(FloatingChatService.suppressesMessagePreview());
        }
    }

    @Test public void chatPagesSuppressOnlyWhileTheAppIsForeground() {
        for (String page : new String[]{"/chat", "/chatroom", "/chatroom/"}) {
            FloatingChatService.setVisiblePage(page);
            FloatingChatService.setAppForeground(true);
            assertTrue(FloatingChatService.suppressesMessagePreview());
            FloatingChatService.setAppForeground(false);
            assertFalse(FloatingChatService.suppressesMessagePreview());
        }
    }
}
