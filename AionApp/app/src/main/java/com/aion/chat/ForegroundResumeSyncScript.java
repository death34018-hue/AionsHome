package com.aion.chat;

final class ForegroundResumeSyncScript {
    private ForegroundResumeSyncScript() {}

    static String nativeMessages() {
        return "(function(){function sync(w){try{w?.onAionNativeMessages?.();}catch(e){}}"
                + "sync(window);document.querySelectorAll('iframe').forEach(f=>sync(f.contentWindow));})();";
    }

    static String build() {
        return nativeMessages() + "(function(){"
                + "try{"
                + "var needsReconnect=(typeof ws==='undefined'||!ws||ws.readyState!==1);"
                + "if(needsReconnect&&typeof connectWS==='function'){"
                + "console.log('[AionApp] WS reconnect on foreground resume');"
                + "connectWS();"
                + "}"
                + "setTimeout(function(){"
                + "if(typeof refreshCurrentConversationFromServer==='function'){"
                + "refreshCurrentConversationFromServer({scroll:true,reason:'android_resume'});"
                + "}"
                + "function refreshChatroom(win){"
                + "try{"
                + "if(win&&typeof win.refreshCurrentChatroomFromServer==='function'){"
                + "win.refreshCurrentChatroomFromServer({scroll:true,reason:'android_resume'});"
                + "}"
                + "}catch(e){}"
                + "}"
                + "refreshChatroom(window);"
                + "document.querySelectorAll('iframe').forEach(function(frame){"
                + "refreshChatroom(frame.contentWindow);"
                + "});"
                + "},300);"
                + "}catch(e){console.warn('[AionApp] foreground sync failed',e);}"
                + "})();";
    }
}
