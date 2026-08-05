package com.aion.chat.homecoming;

import org.junit.Test;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingReturnClientTest {
    @Test
    public void usesOnlyBoundedTypedReturnEndpoints() throws Exception {
        RecordingTransport transport = new RecordingTransport();
        transport.responses.add(response(202,
                "{\"package_id\":\"package-one\",\"state\":\"received\"}"));
        transport.responses.add(response(200,
                "{\"plan_id\":\"plan-one\",\"counts\":{\"apply\":1}}"));
        transport.responses.add(response(200,
                "{\"import_session_id\":\"plan-one\","
                        + "\"package_id\":\"package-one\","
                        + "\"accepted_highest_device_seq\":2,"
                        + "\"counts\":{\"apply\":1},"
                        + "\"result_summary_sha256\":\"summary\","
                        + "\"complete\":true,\"retryable\":false}"));
        transport.responses.add(response(200,
                "{\"package_id\":\"package-one\",\"state\":\"confirmed\","
                        + "\"import_session_id\":\"plan-one\","
                        + "\"accepted_highest_device_seq\":2,"
                        + "\"counts\":{\"apply\":1},"
                        + "\"result_summary_sha256\":\"summary\","
                        + "\"complete\":true}"));
        HomecomingReturnClient client = new HomecomingReturnClient(transport);
        HomecomingReturnPackageRepository.ReturnPackage value = returnPackage();

        client.upload("https://house.example/chat", value);
        client.dryRun("https://house.example/chat", value.packageId);
        HomecomingReturnClient.Receipt applied =
                client.apply("https://house.example/chat", value.packageId);
        HomecomingReturnClient.Receipt status =
                client.status("https://house.example/chat", value.packageId);

        assertEquals("POST https://house.example/api/homecoming/v1/return-packages",
                transport.calls.get(0));
        assertEquals("POST https://house.example/api/homecoming/v1/return-packages/"
                        + "package-one/dry-run", transport.calls.get(1));
        assertEquals("POST https://house.example/api/homecoming/v1/return-packages/"
                        + "package-one/apply", transport.calls.get(2));
        assertEquals("GET https://house.example/api/homecoming/v1/return-packages/"
                        + "package-one", transport.calls.get(3));
        assertArrayEquals(value.compressedEnvelope, transport.firstBody);
        assertEquals("application/gzip", transport.firstContentType);
        assertFalse(transport.headers.toString().toLowerCase().contains("api"));
        assertTrue(applied.complete);
        assertEquals("summary", status.resultSummarySha256);
    }

    @Test
    public void retryableAndPartialReceiptsStayTyped() throws Exception {
        RecordingTransport transport = new RecordingTransport();
        transport.responses.add(response(200,
                "{\"import_session_id\":\"plan-one\","
                        + "\"package_id\":\"package-one\","
                        + "\"accepted_highest_device_seq\":1,"
                        + "\"counts\":{},\"result_summary_sha256\":\"\","
                        + "\"complete\":false,\"retryable\":true}"));
        HomecomingReturnClient.Receipt receipt =
                new HomecomingReturnClient(transport)
                        .apply("http://192.168.1.2:8080/chat", "package-one");

        assertFalse(receipt.complete);
        assertTrue(receipt.retryable);
        assertEquals(1L, receipt.acceptedHighestDeviceSeq);
    }

    private static HomecomingReturnClient.TransportResponse response(
            int code, String body) {
        return new HomecomingReturnClient.TransportResponse(
                code, body.getBytes(StandardCharsets.UTF_8));
    }

    private static HomecomingReturnPackageRepository.ReturnPackage returnPackage() {
        return new HomecomingReturnPackageRepository.ReturnPackage(
                "package-one", "epoch-one", "device-one", "snapshot-one",
                1L, 2L, 2, "hash", "signature",
                new byte[]{1, 2, 3}, "", "ready", 1L, 1L);
    }

    private static final class RecordingTransport
            implements HomecomingReturnClient.Transport {
        final List<String> calls = new ArrayList<>();
        final List<HomecomingReturnClient.TransportResponse> responses =
                new ArrayList<>();
        final Map<String, String> headers = new LinkedHashMap<>();
        int next;
        byte[] firstBody;
        String firstContentType;

        @Override
        public HomecomingReturnClient.TransportResponse execute(
                String method, String url, String contentType, byte[] body,
                Map<String, String> suppliedHeaders) {
            calls.add(method + " " + url);
            if (firstBody == null) {
                firstBody = body.clone();
                firstContentType = contentType;
                headers.putAll(suppliedHeaders);
            }
            return responses.get(next++);
        }

        @Override public void cancel() {
        }
    }
}
