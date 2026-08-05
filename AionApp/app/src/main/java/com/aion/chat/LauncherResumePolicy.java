package com.aion.chat;

/**
 * Keeps repeated launcher intents from rebuilding the existing WebView task.
 */
final class LauncherResumePolicy {

    private LauncherResumePolicy() {
    }

    static boolean shouldFinishWithoutLaunching(
            boolean isTaskRoot,
            boolean forceAddressPicker) {
        return !isTaskRoot && !forceAddressPicker;
    }
}
