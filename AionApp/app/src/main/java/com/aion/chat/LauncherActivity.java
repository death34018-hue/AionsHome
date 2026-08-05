package com.aion.chat;

import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.Bundle;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

import com.aion.chat.homecoming.HomecomingActivity;
import com.aion.chat.homecoming.HomecomingBackupScheduler;
import com.aion.chat.homecoming.HomecomingModeStore;
import com.aion.chat.homecoming.HomecomingReturnPackageRepository;

/**
 * 启动页 — 选择连接地址（家庭WiFi / Cloudflare / 户外Tailscale）
 */
public class LauncherActivity extends AppCompatActivity {
    private HomecomingBackupScheduler homecomingBackupScheduler;

    public static final String EXTRA_FORCE_ADDRESS_PICKER = "force_address_picker";
    private static final String PREFS       = "aion_prefs";
    private static final String KEY_URL     = "saved_url";
    private static final String KEY_AUTO    = "auto_connect";

    // ★ 在这里修改你的连接地址
    private static final String URL_HOME       = "http://192.168.xx.xxx:8080/chat";
    private static final String URL_CLOUDFLARE = ConnectionEndpoint.CLOUDFLARE_PAGE_URL;
    private static final String URL_OUTDOOR    = "http://192.168.xx.xxx:8080/chat";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        boolean forceAddressPicker = getIntent().getBooleanExtra(
                EXTRA_FORCE_ADDRESS_PICKER, false);
        HomecomingModeStore homecomingMode = new HomecomingModeStore(this);
        if (homecomingMode.isActive()
                || homecomingMode.isFrozen()
                || homecomingMode.isFreezing()) {
            startActivity(new Intent(this, HomecomingActivity.class));
            finish();
            return;
        }
        if (LauncherResumePolicy.shouldFinishWithoutLaunching(
                isTaskRoot(), forceAddressPicker)) {
            finish();
            return;
        }

        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        prepareHomecomingBackup();

        // 如果上次勾选了"记住选择"，直接跳转
        if (prefs.getBoolean(KEY_AUTO, false)) {
            String savedUrl = ConnectionEndpoint.normalizePageUrl(
                    prefs.getString(KEY_URL, URL_HOME));
            prefs.edit().putString(KEY_URL, savedUrl).apply();
            refreshHomecomingBackup(savedUrl, false);
            launchWebView(savedUrl);
            return;
        }
        refreshHomecomingBackup(
                ConnectionEndpoint.normalizePageUrl(prefs.getString(KEY_URL, URL_HOME)),
                false);

        setContentView(R.layout.activity_launcher);

        TextView tvHome       = findViewById(R.id.tvHomeUrl);
        TextView tvCloudflare = findViewById(R.id.tvCloudflareUrl);
        TextView tvOutdoor    = findViewById(R.id.tvOutdoorUrl);
        TextView tvHomecomingHint = findViewById(R.id.tvHomecomingHint);
        Button   btnHome      = findViewById(R.id.btnHome);
        Button   btnCloudflare= findViewById(R.id.btnCloudflare);
        Button   btnOutdoor   = findViewById(R.id.btnOutdoor);
        Button   btnHomecoming= findViewById(R.id.btnHomecoming);
        CheckBox cbRemember   = findViewById(R.id.cbRemember);

        tvHome.setText(URL_HOME);
        tvCloudflare.setText(URL_CLOUDFLARE);
        tvOutdoor.setText(URL_OUTDOOR);
        boolean pendingReturn = false;
        try {
            pendingReturn = !new HomecomingReturnPackageRepository(this)
                    .pendingInSequence().isEmpty();
        } catch (Exception ignored) {
            // The normal address picker must remain available.
        }
        final boolean hasPendingReturn = pendingReturn;
        if (hasPendingReturn) {
            btnHomecoming.setText("🏠  归巢模式 · 待回传");
            tvHomecomingHint.setText("有归巢数据等待手动回传");
        }

        btnHome.setOnClickListener(v -> {
            saveIfNeeded(prefs, cbRemember.isChecked(), URL_HOME);
            refreshHomecomingBackup(URL_HOME, true);
            launchWebView(URL_HOME);
        });

        btnCloudflare.setOnClickListener(v -> {
            saveIfNeeded(prefs, cbRemember.isChecked(), URL_CLOUDFLARE);
            refreshHomecomingBackup(URL_CLOUDFLARE, true);
            launchWebView(URL_CLOUDFLARE);
        });

        btnOutdoor.setOnClickListener(v -> {
            saveIfNeeded(prefs, cbRemember.isChecked(), URL_OUTDOOR);
            refreshHomecomingBackup(URL_OUTDOOR, true);
            launchWebView(URL_OUTDOOR);
        });

        btnHomecoming.setOnClickListener(v -> {
            Intent intent = new Intent(this, HomecomingActivity.class);
            intent.putExtra(HomecomingActivity.EXTRA_SHOW_CONFIRMATION, true);
            intent.putExtra(HomecomingActivity.EXTRA_OPEN_RETURN, hasPendingReturn);
            startActivity(intent);
        });
    }

    private void prepareHomecomingBackup() {
        try {
            homecomingBackupScheduler = HomecomingBackupScheduler.create(this);
        } catch (RuntimeException ignored) {
            homecomingBackupScheduler = null;
        }
    }

    private void refreshHomecomingBackup(String url, boolean routeChanged) {
        try {
            if (homecomingBackupScheduler == null) {
                return;
            }
            if (routeChanged) {
                homecomingBackupScheduler.onNormalRouteSelected(url);
            } else {
                homecomingBackupScheduler.onLauncherForeground(url);
            }
        } catch (RuntimeException ignored) {
            // Homecoming backup is best-effort and never blocks normal launch.
        }
    }

    private void saveIfNeeded(SharedPreferences prefs, boolean remember, String url) {
        SharedPreferences.Editor editor = prefs.edit();
        editor.putString(KEY_URL, url);
        editor.putBoolean(KEY_AUTO, remember);
        editor.apply();
    }

    private void launchWebView(String url) {
        url = ConnectionEndpoint.normalizePageUrl(url);
        // 启动前台推送服务
        startPushService(url);

        Intent intent = new Intent(this, WebViewActivity.class);
        intent.putExtra("url", url);
        startActivity(intent);
        finish();
    }

    private void startPushService(String url) {
        // 启动前台服务（权限请求移到 WebViewActivity，因为本 Activity 会立即 finish）
        Intent serviceIntent = new Intent(this, AionPushService.class);
        serviceIntent.putExtra("url", url);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent);
        } else {
            startService(serviceIntent);
        }
    }
}
