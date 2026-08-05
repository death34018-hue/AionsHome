package com.aion.chat.homecoming;

import org.json.JSONObject;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

public final class HomecomingTtsNetworkSynthesizer
        implements HomecomingTtsEngine.Synthesizer {
    private static final MediaType JSON =
            MediaType.get("application/json; charset=utf-8");
    private final HomecomingRouteVault vault;
    private final ExecutorService executor = Executors.newFixedThreadPool(2);
    private final OkHttpClient client = new OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(120, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .build();

    public HomecomingTtsNetworkSynthesizer(HomecomingRouteVault vault) {
        this.vault = vault;
    }

    @Override
    public void synthesize(
            String text, String voiceId, HomecomingTtsEngine.SynthesisCallback callback) {
        executor.execute(() -> {
            try {
                HomecomingRouteVault.Service service = vault.resolveService("tts");
                if (!service.enabled || voiceId == null || voiceId.trim().isEmpty()) {
                    callback.onFailure();
                    return;
                }
                JSONObject payload = new JSONObject()
                        .put("model", service.model)
                        .put("input", text)
                        .put("voice", voiceId)
                        .put("response_format", "mp3");
                Request request = new Request.Builder()
                        .url(service.baseUrl)
                        .header("Authorization", "Bearer " + service.apiKey)
                        .post(RequestBody.create(payload.toString(), JSON))
                        .build();
                try (Response response = client.newCall(request).execute()) {
                    if (!response.isSuccessful() || response.body() == null) {
                        callback.onFailure();
                        return;
                    }
                    byte[] audio = response.body().bytes();
                    if (audio.length == 0 || audio.length > 16 * 1024 * 1024) {
                        callback.onFailure();
                        return;
                    }
                    callback.onSuccess(audio);
                }
            } catch (Exception exception) {
                callback.onFailure();
            }
        });
    }
}
