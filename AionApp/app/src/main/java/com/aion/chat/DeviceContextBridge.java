package com.aion.chat;

import android.app.Activity;
import android.content.Intent;
import android.hardware.Sensor;
import android.hardware.SensorManager;
import android.provider.Settings;
import android.webkit.JavascriptInterface;

import androidx.core.app.NotificationManagerCompat;

import org.json.JSONObject;
import org.json.JSONException;

/** Small settings/diagnostics bridge used by the activity-log page. */
public final class DeviceContextBridge {
    private final Activity activity;

    public DeviceContextBridge(Activity activity) { this.activity = activity; }

    @JavascriptInterface public boolean hasNotificationAccess() {
        return NotificationManagerCompat.getEnabledListenerPackages(activity)
                .contains(activity.getPackageName());
    }

    @JavascriptInterface public void openNotificationAccessSettings() {
        activity.runOnUiThread(() -> activity.startActivity(
                new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)));
    }

    @JavascriptInterface public String getSensorCapabilities() {
        SensorManager manager = (SensorManager) activity.getSystemService(Activity.SENSOR_SERVICE);
        JSONObject result = new JSONObject();
        try {
            result.put("gravity", has(manager, Sensor.TYPE_GRAVITY));
            result.put("accelerometer", has(manager, Sensor.TYPE_ACCELEROMETER));
            result.put("linear_acceleration", has(manager, Sensor.TYPE_LINEAR_ACCELERATION));
            result.put("light", has(manager, Sensor.TYPE_LIGHT));
            result.put("proximity", has(manager, Sensor.TYPE_PROXIMITY));
        } catch (JSONException ignored) {}
        return result.toString();
    }

    private static boolean has(SensorManager manager, int type) {
        return manager != null && manager.getDefaultSensor(type) != null;
    }
}
