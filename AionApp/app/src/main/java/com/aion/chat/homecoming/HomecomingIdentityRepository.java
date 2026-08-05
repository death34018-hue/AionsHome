package com.aion.chat.homecoming;

import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;

import org.json.JSONObject;

import java.util.Arrays;
import java.util.List;

public final class HomecomingIdentityRepository {
    private final Source source;

    public HomecomingIdentityRepository(HomecomingDatabase database) {
        this(new SQLiteSource(database));
    }

    HomecomingIdentityRepository(Source source) {
        this.source = source;
    }

    public Identity identityForTimeline(String timelineId, String responderOwner) {
        String owner;
        if ("main_private".equals(timelineId)) {
            owner = "main";
        } else if ("companion_private".equals(timelineId)) {
            owner = "second";
        } else if ("group".equals(timelineId)) {
            owner = validateOwner(responderOwner);
        } else {
            throw new IllegalArgumentException("unsupported timeline");
        }
        try {
            JSONObject root = new JSONObject(source.load());
            JSONObject user = root.getJSONObject("user");
            JSONObject companion = root.getJSONObject("companions").getJSONObject(owner);
            return new Identity(
                    required(user.optString("name", ""), "configured user name"),
                    user.optString("persona", ""),
                    owner,
                    required(companion.optString("name", ""), "configured companion name"),
                    companion.optString("persona", ""),
                    root.optString("system_prompt", ""));
        } catch (Exception exception) {
            throw new IllegalStateException("verified identity snapshot is invalid", exception);
        }
    }

    public List<String> groupReplyOrder(String turnId) {
        try {
            JSONObject root = new JSONObject(source.load());
            String configured = root.optString("reply_order", "random")
                    .trim().toLowerCase();
            if ("connor".equals(configured) || "second".equals(configured)) {
                return Arrays.asList("second", "main");
            }
            if ("aion".equals(configured) || "main".equals(configured)) {
                return Arrays.asList("main", "second");
            }
            return turnId.hashCode() % 2 == 0
                    ? Arrays.asList("main", "second")
                    : Arrays.asList("second", "main");
        } catch (Exception exception) {
            return Arrays.asList("main", "second");
        }
    }

    interface Source {
        String load();
    }

    private static final class SQLiteSource implements Source {
        private final HomecomingDatabase helper;
        SQLiteSource(HomecomingDatabase helper) { this.helper = helper; }
        @Override public String load() {
            SQLiteDatabase database = helper.getReadableDatabase();
            try (Cursor cursor = database.rawQuery(
                    "SELECT payload_json FROM identity_snapshot WHERE identity_id='identity'",
                    null)) {
                if (!cursor.moveToFirst()) {
                    throw new IllegalStateException("identity snapshot is missing");
                }
                return cursor.getString(0);
            }
        }
    }

    public static final class Identity {
        public final String userName;
        public final String userPersona;
        public final String ownerId;
        public final String companionName;
        public final String companionPersona;
        public final String systemPrompt;

        Identity(String userName, String userPersona, String ownerId,
                String companionName, String companionPersona, String systemPrompt) {
            this.userName = userName;
            this.userPersona = userPersona;
            this.ownerId = ownerId;
            this.companionName = companionName;
            this.companionPersona = companionPersona;
            this.systemPrompt = systemPrompt;
        }
    }

    private static String validateOwner(String owner) {
        if (!"main".equals(owner) && !"second".equals(owner)) {
            throw new IllegalArgumentException("unsupported memory owner");
        }
        return owner;
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }
}
