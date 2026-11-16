package com.aion.chat;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.assertFalse;

public class AttentionCallTest {
    @Test public void warmCallOnlyReusesTheLoadedChatShellOnTheSameServer() {
        String target = "https://home.test/chatroom?room=group";
        assertTrue(WebViewLaunchPolicy.canReuseChatShell("https://home.test/chat?conv=first", target));
        assertFalse(WebViewLaunchPolicy.canReuseChatShell("https://home.test/login", target));
        assertFalse(WebViewLaunchPolicy.canReuseChatShell("https://other.test/chat", target));
        assertFalse(WebViewLaunchPolicy.canReuseChatShell("http://home.test/chat", target));
    }
    @Test
    public void routesBothLiveAndBackgroundCommandsToTheirSourceConversation() {
        assertEquals("/chat?conv=private-1", AttentionCall.conversationPath("private", "private-1"));
        assertEquals("/chat?conv=private-2", AttentionCall.conversationPath("background_private", "private-2"));
        assertEquals("/chatroom?room=room-1", AttentionCall.conversationPath("chatroom", "room-1"));
        assertEquals("/chatroom?room=room-2", AttentionCall.conversationPath("background_chatroom", "room-2"));
        assertEquals("/chatroom?room=room%20%26%3F%23", AttentionCall.conversationPath("chatroom", "room &?#"));
    }
}
