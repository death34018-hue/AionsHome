package com.aion.chat.homecoming;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Before;
import org.junit.Test;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

public class HomecomingModelGatewayTest {
    private HomecomingRouteVault vault;

    @Before
    public void setUp() throws Exception {
        JSONObject openAi = route(
                "openai-route", "custom_openai", "https://relay.example/v1",
                "openai-secret", "model-openai", "vendor/chat", true);
        JSONObject gemini = route(
                "gemini-route", "gemini", "https://generative.example/v1beta",
                "gemini-secret", "model-gemini", "gemini-test", false);
        vault = HomecomingRouteVault.fromPlaintext(new JSONObject()
                .put("chat", new JSONArray().put(openAi).put(gemini))
                .toString().getBytes(StandardCharsets.UTF_8));
    }

    @Test
    public void openAiCompatibleRequestAndChunksAreNativeOnly() throws Exception {
        FakeTransport transport = new FakeTransport(Arrays.asList(
                "data: {\"choices\":[{\"delta\":{\"content\":\"你\"}}]}",
                "data: {\"choices\":[{\"delta\":{\"content\":\"好\"}}]}",
                "data: [DONE]"));
        RecordingObserver observer = new RecordingObserver();
        HomecomingModelGateway gateway = new HomecomingModelGateway(vault, transport);

        gateway.stream(request("openai-route", "model-openai", false), observer);

        assertTrue(transport.request.url.endsWith("/chat/completions"));
        assertEquals("Bearer openai-secret",
                transport.request.headers.get("Authorization"));
        assertTrue(transport.request.body.contains("\"stream\":true"));
        assertEquals(Arrays.asList("你", "好"), observer.chunks);
        assertEquals("你好", observer.complete);
        assertFalse(observer.complete.contains("secret"));
    }

    @Test
    public void geminiUsesProviderShapeAndParsesSse() throws Exception {
        FakeTransport transport = new FakeTransport(Arrays.asList(
                "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"归巢\"}]}}]}"));
        RecordingObserver observer = new RecordingObserver();
        new HomecomingModelGateway(vault, transport)
                .stream(request("gemini-route", "model-gemini", false), observer);

        assertTrue(transport.request.url.contains(
                "/models/gemini-test:streamGenerateContent"));
        assertEquals("gemini-secret", transport.request.headers.get("x-goog-api-key"));
        assertTrue(transport.request.body.contains("\"contents\""));
        assertEquals("归巢", observer.complete);
    }

    @Test
    public void visionInputIsRejectedBeforeNetworkForTextOnlyModel() {
        FakeTransport transport = new FakeTransport(new ArrayList<>());
        HomecomingModelGateway gateway = new HomecomingModelGateway(vault, transport);

        assertThrows(IllegalArgumentException.class,
                () -> gateway.stream(
                        request("gemini-route", "model-gemini", true),
                        new RecordingObserver()));
        assertEquals(0, transport.calls);
    }

    @Test
    public void cancellationStopsChunksAndDoesNotComplete() throws Exception {
        RecordingObserver observer = new RecordingObserver();
        final HomecomingModelGateway[] holder = new HomecomingModelGateway[1];
        FakeTransport transport = new FakeTransport(Arrays.asList("one", "two")) {
            @Override
            public void execute(HomecomingModelGateway.NetworkRequest request,
                    HomecomingModelGateway.ChunkConsumer consumer,
                    HomecomingModelGateway.CancelSignal cancel) throws Exception {
                calls++;
                consumer.accept("data: {\"choices\":[{\"delta\":{\"content\":\"first\"}}]}");
                holder[0].cancel("request-one");
                if (!cancel.isCancelled()) {
                    consumer.accept("data: {\"choices\":[{\"delta\":{\"content\":\"second\"}}]}");
                }
            }
        };
        holder[0] = new HomecomingModelGateway(vault, transport);

        holder[0].stream(request("openai-route", "model-openai", false), observer);

        assertEquals(Arrays.asList("first"), observer.chunks);
        assertEquals(null, observer.complete);
        assertEquals("cancelled", observer.failure);
    }

    private static HomecomingModelGateway.ChatRequest request(
            String route, String model, boolean image) {
        return new HomecomingModelGateway.ChatRequest(
                "request-one", route, model,
                Arrays.asList(
                        new HomecomingModelGateway.ChatMessage("system", "persona"),
                        new HomecomingModelGateway.ChatMessage("user", "hello")),
                image ? "data:image/jpeg;base64,fixture" : "");
    }

    private static JSONObject route(String id, String provider, String base, String key,
            String modelKey, String modelName, boolean vision) throws Exception {
        return new JSONObject()
                .put("route_id", id)
                .put("label", id)
                .put("provider", provider)
                .put("base_url", base)
                .put("api_key", key)
                .put("models", new JSONArray().put(new JSONObject()
                        .put("key", modelKey)
                        .put("model", modelName)
                        .put("vision", vision)
                        .put("audio", false)));
    }

    private static class FakeTransport implements HomecomingModelGateway.Transport {
        final List<String> lines;
        HomecomingModelGateway.NetworkRequest request;
        int calls;

        FakeTransport(List<String> lines) {
            this.lines = lines;
        }

        @Override
        public void execute(HomecomingModelGateway.NetworkRequest request,
                HomecomingModelGateway.ChunkConsumer consumer,
                HomecomingModelGateway.CancelSignal cancel) throws Exception {
            this.request = request;
            calls++;
            for (String line : lines) {
                if (cancel.isCancelled()) {
                    return;
                }
                consumer.accept(line);
            }
        }
    }

    private static final class RecordingObserver
            implements HomecomingModelGateway.StreamObserver {
        final List<String> chunks = new ArrayList<>();
        String complete;
        String failure;
        @Override public void onChunk(String text) { chunks.add(text); }
        @Override public void onComplete(String text) { complete = text; }
        @Override public void onFailure(String code) { failure = code; }
    }
}
