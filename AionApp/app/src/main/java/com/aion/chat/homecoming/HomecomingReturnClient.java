package com.aion.chat.homecoming;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import okhttp3.Call;
import okhttp3.HttpUrl;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import okhttp3.ResponseBody;

public final class HomecomingReturnClient
        implements HomecomingReturnController.ClientPort {
    private static final int MAX_RESPONSE_BYTES = 1024 * 1024;
    private final Transport transport;

    public HomecomingReturnClient() {
        this(new OkHttpTransport());
    }

    HomecomingReturnClient(Transport transport) {
        if (transport == null) {
            throw new IllegalArgumentException("transport is required");
        }
        this.transport = transport;
    }

    public ServerState upload(
            String serverPageUrl,
            HomecomingReturnPackageRepository.ReturnPackage value) throws Exception {
        Map<String, String> headers = new LinkedHashMap<>();
        headers.put("X-Homecoming-Device-Id", value.deviceId);
        headers.put("X-Homecoming-Package-Id", value.packageId);
        JSONObject body = executeJson(
                "POST",
                endpoint(serverPageUrl, "return-packages"),
                "application/gzip",
                value.compressedEnvelope,
                headers,
                202);
        return new ServerState(
                required(body.optString("package_id", ""), "package_id"),
                required(body.optString("state", ""), "state"));
    }

    public Plan dryRun(String serverPageUrl, String packageId) throws Exception {
        JSONObject body = executeJson(
                "POST",
                endpoint(serverPageUrl, "return-packages/"
                        + pathId(packageId) + "/dry-run"),
                "application/json",
                new byte[0],
                Collections.<String, String>emptyMap(),
                200);
        return new Plan(
                required(body.optString("plan_id", ""), "plan_id"),
                integerMap(body.optJSONObject("counts")));
    }

    public Receipt apply(String serverPageUrl, String packageId) throws Exception {
        JSONObject body = executeJson(
                "POST",
                endpoint(serverPageUrl, "return-packages/"
                        + pathId(packageId) + "/apply"),
                "application/json",
                new byte[0],
                Collections.<String, String>emptyMap(),
                200);
        return receipt(body, packageId);
    }

    public Receipt status(String serverPageUrl, String packageId) throws Exception {
        JSONObject body = executeJson(
                "GET",
                endpoint(serverPageUrl, "return-packages/" + pathId(packageId)),
                "",
                new byte[0],
                Collections.<String, String>emptyMap(),
                200);
        return receipt(body, packageId);
    }

    public void cancel() {
        transport.cancel();
    }

    private JSONObject executeJson(
            String method, String url, String contentType, byte[] requestBody,
            Map<String, String> headers, int expectedStatus) throws Exception {
        TransportResponse response = transport.execute(
                method, url, contentType, requestBody, headers);
        if (response.statusCode != expectedStatus) {
            throw new IOException("return HTTP " + response.statusCode);
        }
        if (response.body.length == 0 || response.body.length > MAX_RESPONSE_BYTES) {
            throw new IOException("return response size is invalid");
        }
        return new JSONObject(new String(response.body, StandardCharsets.UTF_8));
    }

    private static Receipt receipt(JSONObject body, String expectedPackageId)
            throws Exception {
        String packageId = required(
                body.optString("package_id", expectedPackageId), "package_id");
        return new Receipt(
                body.optString("import_session_id", ""),
                packageId,
                body.optLong("accepted_highest_device_seq", 0L),
                integerMap(body.optJSONObject("counts")),
                body.optString("result_summary_sha256", ""),
                body.optBoolean("complete", false),
                body.optBoolean("retryable", false));
    }

    private static Map<String, Integer> integerMap(JSONObject value) {
        if (value == null) return Collections.emptyMap();
        LinkedHashMap<String, Integer> result = new LinkedHashMap<>();
        java.util.Iterator<String> keys = value.keys();
        while (keys.hasNext()) {
            String key = keys.next();
            result.put(key, value.optInt(key, 0));
        }
        return Collections.unmodifiableMap(result);
    }

    private static String endpoint(String pageUrl, String suffix) throws IOException {
        HttpUrl page = HttpUrl.parse(pageUrl);
        if (page == null || (!"https".equals(page.scheme())
                && !"http".equals(page.scheme()))) {
            throw new IOException("invalid return server URL");
        }
        return page.newBuilder()
                .encodedPath("/api/homecoming/v1/" + suffix)
                .query(null)
                .fragment(null)
                .build()
                .toString();
    }

    private static String pathId(String value) {
        String checked = required(value, "packageId");
        if (!checked.matches("[A-Za-z0-9._:-]{1,200}")) {
            throw new IllegalArgumentException("invalid packageId");
        }
        return checked;
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }

    interface Transport {
        TransportResponse execute(
                String method, String url, String contentType, byte[] body,
                Map<String, String> headers) throws Exception;
        void cancel();
    }

    static final class TransportResponse {
        final int statusCode;
        final byte[] body;

        TransportResponse(int statusCode, byte[] body) {
            this.statusCode = statusCode;
            this.body = body == null ? new byte[0] : body.clone();
        }
    }

    static final class OkHttpTransport implements Transport {
        private final OkHttpClient client = new OkHttpClient.Builder()
                .connectTimeout(10, TimeUnit.SECONDS)
                .readTimeout(30, TimeUnit.SECONDS)
                .writeTimeout(30, TimeUnit.SECONDS)
                .build();
        private volatile Call inFlight;

        @Override
        public TransportResponse execute(
                String method, String url, String contentType, byte[] body,
                Map<String, String> headers) throws Exception {
            Request.Builder request = new Request.Builder().url(url);
            for (Map.Entry<String, String> header : headers.entrySet()) {
                request.header(header.getKey(), header.getValue());
            }
            if ("GET".equals(method)) {
                request.get();
            } else {
                MediaType type = MediaType.get(contentType);
                request.method(method, RequestBody.create(body, type));
            }
            Call call = client.newCall(request.build());
            inFlight = call;
            try (Response response = call.execute()) {
                ResponseBody responseBody = response.body();
                byte[] bytes = responseBody == null
                        ? new byte[0] : readCapped(
                                responseBody.byteStream(), MAX_RESPONSE_BYTES);
                return new TransportResponse(response.code(), bytes);
            } finally {
                inFlight = null;
            }
        }

        @Override public void cancel() {
            Call call = inFlight;
            if (call != null) call.cancel();
        }

        private static byte[] readCapped(InputStream input, int limit)
                throws IOException {
            try {
                ByteArrayOutputStream output = new ByteArrayOutputStream();
                byte[] buffer = new byte[8192];
                int count;
                while ((count = input.read(buffer)) != -1) {
                    if (output.size() + count > limit) {
                        throw new IOException("return response is too large");
                    }
                    output.write(buffer, 0, count);
                }
                return output.toByteArray();
            } finally {
                input.close();
            }
        }
    }

    public static final class ServerState {
        public final String packageId;
        public final String state;

        public ServerState(String packageId, String state) {
            this.packageId = packageId;
            this.state = state;
        }
    }

    public static final class Plan {
        public final String planId;
        public final Map<String, Integer> counts;

        public Plan(String planId, Map<String, Integer> counts) {
            this.planId = planId;
            this.counts = Collections.unmodifiableMap(
                    new LinkedHashMap<>(counts));
        }
    }

    public static final class Receipt {
        public final String importSessionId;
        public final String packageId;
        public final long acceptedHighestDeviceSeq;
        public final Map<String, Integer> counts;
        public final String resultSummarySha256;
        public final boolean complete;
        public final boolean retryable;

        public Receipt(
                String importSessionId,
                String packageId,
                long acceptedHighestDeviceSeq,
                Map<String, Integer> counts,
                String resultSummarySha256,
                boolean complete,
                boolean retryable) {
            this.importSessionId = importSessionId == null ? "" : importSessionId;
            this.packageId = packageId == null ? "" : packageId;
            this.acceptedHighestDeviceSeq = acceptedHighestDeviceSeq;
            this.counts = Collections.unmodifiableMap(
                    new LinkedHashMap<>(counts));
            this.resultSummarySha256 =
                    resultSummarySha256 == null ? "" : resultSummarySha256;
            this.complete = complete;
            this.retryable = retryable;
        }
    }
}
