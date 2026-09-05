package com.aion.chat.widget;

import android.content.Context;
import android.content.SharedPreferences;
import android.webkit.CookieManager;

import org.json.JSONObject;

import java.io.File;
import java.io.IOException;
import java.util.concurrent.TimeUnit;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.HttpUrl;
import okhttp3.MediaType;
import okhttp3.MultipartBody;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

final class WidgetAsrClient {
    interface Listener {
        void onText(String text);
        void onError(String message);
    }

    private final Context context;
    private final OkHttpClient client = new OkHttpClient.Builder()
            .connectTimeout(12, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .build();
    private Call call;

    WidgetAsrClient(Context context) {
        this.context = context.getApplicationContext();
    }

    void transcribe(File wav, Listener listener) {
        SharedPreferences prefs = context.getSharedPreferences("aion_prefs", Context.MODE_PRIVATE);
        String savedPageUrl = prefs.getString("saved_url", "");
        HttpUrl pageUrl = savedPageUrl == null ? null : HttpUrl.parse(savedPageUrl);
        if (pageUrl == null) {
            listener.onError("请先打开 App 选择连接地址");
            return;
        }
        HttpUrl endpoint = pageUrl.newBuilder()
                .encodedPath("/api/voice/transcribe")
                .query(null)
                .fragment(null)
                .build();
        RequestBody fileBody = RequestBody.create(wav, MediaType.get("audio/wav"));
        RequestBody body = new MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("file", "widget-memo.wav", fileBody)
                .build();
        Request.Builder request = new Request.Builder().url(endpoint).post(body);
        String cookie = CookieManager.getInstance().getCookie(savedPageUrl);
        if (cookie != null && !cookie.trim().isEmpty()) request.header("Cookie", cookie);

        call = client.newCall(request.build());
        call.enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException e) {
                listener.onError("转写失败，请检查网络后重试");
            }

            @Override
            public void onResponse(Call call, Response response) {
                try (Response closeable = response) {
                    if (!response.isSuccessful() || response.body() == null) {
                        listener.onError("语音服务暂时不可用");
                        return;
                    }
                    JSONObject json = new JSONObject(response.body().string());
                    String text = json.optString("text", "").trim();
                    if (text.isEmpty()) listener.onError("没有听清，再试一次吧");
                    else listener.onText(text);
                } catch (Exception e) {
                    listener.onError("转写结果读取失败");
                }
            }
        });
    }

    void cancel() {
        if (call != null) call.cancel();
        call = null;
    }
}
