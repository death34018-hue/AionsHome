package com.aion.chat.supervision;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.LinkedHashMap;
import java.util.Map;

public final class AppSupervisionRoleCatalog {
    private AppSupervisionRoleCatalog() {}

    public static Map<String, String> fromRuntimeConfig(String raw) throws Exception {
        LinkedHashMap<String, String> labels = new LinkedHashMap<>();
        JSONArray roles = new JSONObject(raw == null ? "{}" : raw)
                .optJSONArray("roles");
        if (roles == null) return labels;
        for (int index = 0; index < roles.length(); index++) {
            JSONObject role = roles.optJSONObject(index);
            if (role == null) continue;
            String id = role.optString("id", "").trim();
            String label = role.optString("label", "").trim();
            if (!id.isEmpty() && !label.isEmpty()) labels.put(id, label);
        }
        return labels;
    }
}
