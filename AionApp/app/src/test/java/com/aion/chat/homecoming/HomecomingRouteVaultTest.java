package com.aion.chat.homecoming;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

import java.nio.charset.StandardCharsets;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;

public class HomecomingRouteVaultTest {
    @Test
    public void projectionNeverContainsCredentialsOrBaseUrl() throws Exception {
        HomecomingRouteVault vault = HomecomingRouteVault.fromPlaintext(
                fixture().toString().getBytes(StandardCharsets.UTF_8));

        JSONObject projected = vault.listDescriptors().get(0).toJson();

        assertEquals("Fixture Cloud", projected.getString("label"));
        assertFalse(projected.toString().contains("fixture-secret"));
        assertFalse(projected.toString().contains("fixture.example"));
        assertFalse(projected.has("api_key"));
        assertFalse(projected.has("base_url"));
    }

    @Test
    public void nativeResolutionReturnsImmutableSecretRecord() throws Exception {
        HomecomingRouteVault vault = HomecomingRouteVault.fromPlaintext(
                fixture().toString().getBytes(StandardCharsets.UTF_8));

        HomecomingRouteVault.Route route = vault.resolve("fixture-cloud");

        assertEquals("fixture-secret", route.apiKey);
        assertEquals("https://fixture.example/v1", route.baseUrl);
        assertEquals("fixture/model", route.models.get(0).model);
        assertThrows(UnsupportedOperationException.class,
                () -> route.models.add(route.models.get(0)));
    }

    @Test
    public void unknownRouteCannotBeResolved() throws Exception {
        HomecomingRouteVault vault = HomecomingRouteVault.fromPlaintext(
                fixture().toString().getBytes(StandardCharsets.UTF_8));
        assertThrows(IllegalArgumentException.class, () -> vault.resolve("missing"));
    }

    private static JSONObject fixture() throws Exception {
        return new JSONObject().put("chat", new JSONArray().put(new JSONObject()
                .put("route_id", "fixture-cloud")
                .put("label", "Fixture Cloud")
                .put("provider", "custom_openai")
                .put("base_url", "https://fixture.example/v1")
                .put("api_key", "fixture-secret")
                .put("models", new JSONArray().put(new JSONObject()
                        .put("key", "fixture-model")
                        .put("model", "fixture/model")
                        .put("vision", true)
                        .put("audio", false)))));
    }
}
