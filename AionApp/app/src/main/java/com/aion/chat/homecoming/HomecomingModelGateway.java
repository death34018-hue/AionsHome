package com.aion.chat.homecoming;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.io.BufferedReader;
import java.io.InputStreamReader;

import okhttp3.Call;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

public final class HomecomingModelGateway {
    private final HomecomingRouteVault vault;
    private final Transport transport;
    private final Map<String, AtomicBoolean> cancellations = new ConcurrentHashMap<>();

    public HomecomingModelGateway(HomecomingRouteVault vault, Transport transport) {
        this.vault = vault;
        this.transport = transport;
    }

    public void stream(ChatRequest request, StreamObserver observer) throws Exception {
        HomecomingRouteVault.Route route = vault.resolve(request.routeId);
        HomecomingRouteVault.Model model = route.model(request.modelKey);
        if (!request.imageDataUrl.isEmpty() && !model.vision) {
            throw new IllegalArgumentException("selected model does not support vision");
        }
        AtomicBoolean cancelled = new AtomicBoolean(false);
        if (cancellations.putIfAbsent(request.requestId, cancelled) != null) {
            throw new IllegalStateException("request is already running");
        }
        StringBuilder complete = new StringBuilder();
        try {
            NetworkRequest network = build(route, model, request);
            transport.execute(network, line -> {
                if (cancelled.get()) {
                    return;
                }
                String chunk = parseChunk(route.provider, line);
                if (!chunk.isEmpty()) {
                    complete.append(chunk);
                    observer.onChunk(chunk);
                }
            }, cancelled::get);
            if (cancelled.get()) {
                observer.onFailure("cancelled");
            } else {
                observer.onComplete(complete.toString());
            }
        } catch (Exception exception) {
            if (cancelled.get()) {
                observer.onFailure("cancelled");
            } else {
                observer.onFailure("model_request_failed");
            }
        } finally {
            cancellations.remove(request.requestId, cancelled);
        }
    }

    public void cancel(String requestId) {
        AtomicBoolean signal = cancellations.get(requestId);
        if (signal != null) {
            signal.set(true);
        }
    }

    public void cancelAll() {
        for (AtomicBoolean signal : cancellations.values()) {
            signal.set(true);
        }
    }

    private static NetworkRequest build(
            HomecomingRouteVault.Route route,
            HomecomingRouteVault.Model model,
            ChatRequest request) throws Exception {
        if ("gemini".equals(route.provider)) {
            return buildGemini(route, model, request);
        }
        if ("siliconflow".equals(route.provider)
                || "custom_openai".equals(route.provider)) {
            return buildOpenAi(route, model, request);
        }
        throw new IllegalArgumentException("unsupported provider");
    }

    private static NetworkRequest buildOpenAi(
            HomecomingRouteVault.Route route,
            HomecomingRouteVault.Model model,
            ChatRequest request) throws Exception {
        JSONArray messages = new JSONArray();
        for (ChatMessage message : request.messages) {
            JSONObject value = new JSONObject().put("role", message.role);
            if (!request.imageDataUrl.isEmpty() && "user".equals(message.role)
                    && message == request.messages.get(request.messages.size() - 1)) {
                value.put("content", new JSONArray()
                        .put(new JSONObject()
                                .put("type", "text")
                                .put("text", message.text))
                        .put(new JSONObject()
                                .put("type", "image_url")
                                .put("image_url", new JSONObject()
                                        .put("url", request.imageDataUrl))));
            } else {
                value.put("content", message.text);
            }
            messages.put(value);
        }
        JSONObject body = new JSONObject()
                .put("model", model.model)
                .put("messages", messages)
                .put("stream", true);
        Map<String, String> headers = new LinkedHashMap<>();
        headers.put("Authorization", "Bearer " + route.apiKey);
        headers.put("Content-Type", "application/json");
        return new NetworkRequest(
                trimSlash(route.baseUrl) + "/chat/completions",
                HomecomingSnapshotStore.canonicalJson(body),
                headers);
    }

    private static NetworkRequest buildGemini(
            HomecomingRouteVault.Route route,
            HomecomingRouteVault.Model model,
            ChatRequest request) throws Exception {
        JSONArray contents = new JSONArray();
        StringBuilder system = new StringBuilder();
        for (ChatMessage message : request.messages) {
            if ("system".equals(message.role)) {
                if (system.length() > 0) {
                    system.append('\n');
                }
                system.append(message.text);
                continue;
            }
            JSONArray parts = new JSONArray().put(new JSONObject().put("text", message.text));
            contents.put(new JSONObject()
                    .put("role", "assistant".equals(message.role) ? "model" : "user")
                    .put("parts", parts));
        }
        JSONObject body = new JSONObject().put("contents", contents);
        if (system.length() > 0) {
            body.put("systemInstruction", new JSONObject()
                    .put("parts", new JSONArray().put(
                            new JSONObject().put("text", system.toString()))));
        }
        Map<String, String> headers = new LinkedHashMap<>();
        headers.put("x-goog-api-key", route.apiKey);
        headers.put("Content-Type", "application/json");
        return new NetworkRequest(
                trimSlash(route.baseUrl) + "/models/" + model.model
                        + ":streamGenerateContent?alt=sse",
                HomecomingSnapshotStore.canonicalJson(body),
                headers);
    }

    private static String parseChunk(String provider, String line) throws Exception {
        String value = line == null ? "" : line.trim();
        if (value.isEmpty() || !value.startsWith("data:")) {
            return "";
        }
        value = value.substring(5).trim();
        if (value.isEmpty() || "[DONE]".equals(value)) {
            return "";
        }
        JSONObject json = new JSONObject(value);
        if ("gemini".equals(provider)) {
            JSONArray candidates = json.optJSONArray("candidates");
            if (candidates == null || candidates.length() == 0) {
                return "";
            }
            JSONArray parts = candidates.getJSONObject(0)
                    .getJSONObject("content").optJSONArray("parts");
            return parts == null || parts.length() == 0
                    ? "" : parts.getJSONObject(0).optString("text", "");
        }
        JSONArray choices = json.optJSONArray("choices");
        return choices == null || choices.length() == 0
                ? "" : choices.getJSONObject(0)
                        .optJSONObject("delta").optString("content", "");
    }

    private static String trimSlash(String value) {
        return value.endsWith("/") ? value.substring(0, value.length() - 1) : value;
    }

    public interface Transport {
        void execute(
                NetworkRequest request,
                ChunkConsumer consumer,
                CancelSignal cancel) throws Exception;
    }

    public interface ChunkConsumer {
        void accept(String line) throws Exception;
    }

    public interface CancelSignal {
        boolean isCancelled();
    }

    public interface StreamObserver {
        void onChunk(String text);
        void onComplete(String text);
        void onFailure(String code);
    }

    public static final class OkHttpSseTransport implements Transport {
        private static final MediaType JSON =
                MediaType.get("application/json; charset=utf-8");
        private final OkHttpClient client = new OkHttpClient.Builder()
                .connectTimeout(10, TimeUnit.SECONDS)
                .readTimeout(120, TimeUnit.SECONDS)
                .writeTimeout(30, TimeUnit.SECONDS)
                .build();

        @Override
        public void execute(NetworkRequest network, ChunkConsumer consumer,
                CancelSignal cancel) throws Exception {
            Request.Builder builder = new Request.Builder()
                    .url(network.url)
                    .post(RequestBody.create(network.body, JSON));
            for (Map.Entry<String, String> header : network.headers.entrySet()) {
                builder.header(header.getKey(), header.getValue());
            }
            Call call = client.newCall(builder.build());
            try (Response response = call.execute()) {
                if (!response.isSuccessful() || response.body() == null) {
                    throw new IllegalStateException("model HTTP " + response.code());
                }
                BufferedReader reader = new BufferedReader(new InputStreamReader(
                        response.body().byteStream(), java.nio.charset.StandardCharsets.UTF_8));
                String line;
                while ((line = reader.readLine()) != null) {
                    if (cancel.isCancelled()) {
                        call.cancel();
                        return;
                    }
                    consumer.accept(line);
                }
            }
        }
    }

    public static final class NetworkRequest {
        public final String url;
        public final String body;
        public final Map<String, String> headers;

        NetworkRequest(String url, String body, Map<String, String> headers) {
            this.url = url;
            this.body = body;
            this.headers = Collections.unmodifiableMap(new LinkedHashMap<>(headers));
        }
    }

    public static final class ChatRequest {
        public final String requestId;
        public final String routeId;
        public final String modelKey;
        public final List<ChatMessage> messages;
        public final String imageDataUrl;

        public ChatRequest(String requestId, String routeId, String modelKey,
                List<ChatMessage> messages, String imageDataUrl) {
            this.requestId = required(requestId, "requestId");
            this.routeId = required(routeId, "routeId");
            this.modelKey = required(modelKey, "modelKey");
            this.messages = Collections.unmodifiableList(new ArrayList<>(messages));
            if (this.messages.isEmpty()) {
                throw new IllegalArgumentException("messages are required");
            }
            this.imageDataUrl = imageDataUrl == null ? "" : imageDataUrl;
        }
    }

    public static final class ChatMessage {
        public final String role;
        public final String text;

        public ChatMessage(String role, String text) {
            this.role = required(role, "role");
            this.text = text == null ? "" : text;
        }

        @Override
        public String toString() {
            return role + ":" + text;
        }
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }
}
