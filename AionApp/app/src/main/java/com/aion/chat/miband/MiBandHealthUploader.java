package com.aion.chat.miband;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.List;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

public final class MiBandHealthUploader implements MiBandRuntime.SampleSink {
    public interface BaseUrlProvider { String get(); }

    private static final MediaType JSON = MediaType.get("application/json; charset=utf-8");
    private static final int BATCH_SIZE = 500;
    private final OkHttpClient client;
    private final BaseUrlProvider baseUrlProvider;

    public MiBandHealthUploader(OkHttpClient client, BaseUrlProvider baseUrlProvider) {
        this.client = client;
        this.baseUrlProvider = baseUrlProvider;
    }

    @Override public void upload(String deviceName, List<MiBandProtocol.ActivitySample> samples)
            throws Exception {
        String base = baseUrlProvider.get();
        if (base == null || base.isEmpty()) throw new IllegalStateException("AionsHome 服务地址尚未就绪");
        for (int offset = 0; offset < samples.size(); offset += BATCH_SIZE) {
            JSONArray items = new JSONArray();
            int end = Math.min(samples.size(), offset + BATCH_SIZE);
            for (int index = offset; index < end; index++) {
                MiBandProtocol.ActivitySample sample = samples.get(index);
                JSONObject item = new JSONObject();
                item.put("measured_at", sample.timestampMillis / 1000.0);
                item.put("raw_kind", sample.rawKind);
                item.put("intensity", sample.intensity);
                item.put("steps", sample.steps);
                item.put("heart_rate", sample.heartRate);
                item.put("unknown", sample.unknown);
                item.put("sleep", sample.sleep);
                item.put("deep_sleep", sample.deepSleep);
                item.put("rem_sleep", sample.remSleep);
                if (sample.sleepStage != null) item.put("sleep_stage", sample.sleepStage);
                items.put(item);
            }
            JSONObject body = new JSONObject();
            body.put("device_name", deviceName);
            body.put("samples", items);
            Request request = new Request.Builder()
                    .url(base + "/api/health/mi-band/activity-batch")
                    .post(RequestBody.create(body.toString(), JSON))
                    .build();
            try (Response response = client.newCall(request).execute()) {
                if (!response.isSuccessful()) {
                    throw new IllegalStateException("上传手环 activity 失败: HTTP " + response.code());
                }
            }
        }
    }
}
