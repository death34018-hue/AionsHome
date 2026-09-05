package com.aion.chat.widget;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;

import java.io.File;

final class WidgetStateStore {
    private static final String PREFS = "widget_control_state";

    private final SharedPreferences prefs;

    WidgetStateStore(Context context) {
        prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    String currentState(String actorId) {
        return prefs.getString("state_" + actorId, "");
    }

    String assetPath(String actorId) {
        String path = prefs.getString("asset_path_" + actorId, "");
        return path != null && new File(path).isFile() ? path : "";
    }

    String assetVersion(String actorId) {
        return prefs.getString("asset_version_" + actorId, "");
    }

    String bannerAssetPath() {
        String path = prefs.getString("banner_asset_path", "");
        return path != null && new File(path).isFile() ? path : "";
    }

    String bannerAssetVersion() {
        return prefs.getString("banner_asset_version", "");
    }

    void applyBannerAsset(String path, String version) {
        if (path == null || path.isEmpty()) return;
        prefs.edit().putString("banner_asset_path", path)
                .putString("banner_asset_version", version == null ? "" : version)
                .apply();
    }

    void applyActor(String actorId, String state, JSONArray states,
                    String assetPath, String assetVersion) {
        SharedPreferences.Editor edit = prefs.edit()
                .putString("state_" + actorId, state == null ? "" : state)
                .putString("states_" + actorId, states == null ? "[]" : states.toString());
        if (assetPath != null && !assetPath.isEmpty()) {
            edit.putString("asset_path_" + actorId, assetPath)
                    .putString("asset_version_" + actorId,
                            assetVersion == null ? "" : assetVersion);
        }
        edit.apply();
    }

    String nextState(String actorId) {
        try {
            JSONArray states = new JSONArray(prefs.getString("states_" + actorId, "[]"));
            if (states.length() == 0) return "";
            String current = currentState(actorId);
            int index = -1;
            for (int i = 0; i < states.length(); i++) {
                if (current.equals(states.optString(i))) {
                    index = i;
                    break;
                }
            }
            return states.optString((index + 1) % states.length(), "");
        } catch (Exception ignored) {
            return "";
        }
    }

    void queueActorState(String actorId, String state) {
        prefs.edit().putString("pending_actor_" + actorId, state).apply();
    }

    String pendingActorState(String actorId) {
        return prefs.getString("pending_actor_" + actorId, "");
    }

    void clearPendingActorState(String actorId, String expectedState) {
        if (expectedState.equals(pendingActorState(actorId))) {
            prefs.edit().remove("pending_actor_" + actorId).apply();
        }
    }

    void queueBannerClear() {
        prefs.edit().putBoolean("pending_banner_clear", true).apply();
    }

    boolean pendingBannerClear() {
        return prefs.getBoolean("pending_banner_clear", false);
    }

    void clearPendingBannerClear() {
        prefs.edit().remove("pending_banner_clear").apply();
    }

    boolean hasPending() {
        return pendingBannerClear()
                || !pendingActorState("aion").isEmpty()
                || !pendingActorState("connor").isEmpty();
    }
}
