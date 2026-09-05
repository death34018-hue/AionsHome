package com.aion.chat.widget;

import java.io.File;

final class WidgetAssetSyncDecision {
    private WidgetAssetSyncDecision() {}

    static boolean needsDownload(String cachedVersion, String cachedPath,
                                 String serverVersion) {
        return serverVersion == null || serverVersion.isEmpty()
                || !serverVersion.equals(cachedVersion)
                || cachedPath == null || cachedPath.isEmpty()
                || !new File(cachedPath).isFile();
    }
}
