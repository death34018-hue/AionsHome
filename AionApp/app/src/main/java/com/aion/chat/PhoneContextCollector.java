package com.aion.chat;

import android.content.Context;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.os.Handler;
import android.os.Looper;

import org.json.JSONObject;

/** Screen-aware, low-rate sensor owner. Network delivery is delegated to the push service. */
public final class PhoneContextCollector implements SensorEventListener {
    public interface Sink { void onSnapshot(JSONObject snapshot); }

    private static final int SAMPLE_US = 150_000;
    private static final int BATCH_US = 1_000_000;
    private static final long COALESCE_MS = 3_000L;
    private static final long HEARTBEAT_MS = 5 * 60_000L;

    private final SensorManager sensors;
    private final DeviceContextClassifier classifier = new DeviceContextClassifier();
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Sink sink;
    private final Sensor gravitySensor;
    private final Sensor accelerometer;
    private final Sensor linearAcceleration;
    private final Sensor lightSensor;
    private final Sensor proximitySensor;
    private final float[] gravityEstimate = new float[3];
    private boolean running;
    private boolean closed;
    private boolean uploadPending;

    private final Runnable emitRunnable = this::emitNow;

    private void emitNow() {
        uploadPending = false;
        if (closed) return;
        long now = System.currentTimeMillis();
        if (!classifier.shouldEmit(now)) return;
        sink.onSnapshot(classifier.snapshot(now));
        classifier.markEmitted(now);
    }

    private final Runnable heartbeatRunnable = new Runnable() {
        @Override public void run() {
            if (closed) return;
            scheduleEmit(0L);
            if (running) handler.postDelayed(this, HEARTBEAT_MS);
        }
    };

    public PhoneContextCollector(Context context, Sink sink) {
        this.sink = sink;
        sensors = (SensorManager) context.getSystemService(Context.SENSOR_SERVICE);
        gravitySensor = sensor(Sensor.TYPE_GRAVITY);
        accelerometer = sensor(Sensor.TYPE_ACCELEROMETER);
        linearAcceleration = sensor(Sensor.TYPE_LINEAR_ACCELERATION);
        lightSensor = sensor(Sensor.TYPE_LIGHT);
        proximitySensor = sensor(Sensor.TYPE_PROXIMITY);
    }

    private Sensor sensor(int type) {
        return sensors == null ? null : sensors.getDefaultSensor(type);
    }

    public void startForScreenOn() {
        if (closed) return;
        classifier.updateScreen(true, System.currentTimeMillis());
        if (!running && sensors != null) {
            running = true;
            register(gravitySensor != null ? gravitySensor : accelerometer);
            if (linearAcceleration != null) register(linearAcceleration);
            register(lightSensor);
            register(proximitySensor);
            handler.removeCallbacks(heartbeatRunnable);
            handler.postDelayed(heartbeatRunnable, HEARTBEAT_MS);
        }
        scheduleEmit(COALESCE_MS);
    }

    public void onScreenOff() {
        classifier.updateScreen(false, System.currentTimeMillis());
        scheduleEmit(0L);
        handler.postDelayed(this::stopContinuousMotion, 5_000L);
    }

    public void stopContinuousMotion() {
        if (!running || sensors == null) return;
        sensors.unregisterListener(this);
        running = false;
        handler.removeCallbacks(heartbeatRunnable);
    }

    public void setForegroundApp(String packageName) {
        classifier.updateForegroundApp(packageName, System.currentTimeMillis());
        scheduleEmit(COALESCE_MS);
    }

    private void register(Sensor sensor) {
        if (sensor != null) sensors.registerListener(this, sensor, SAMPLE_US, BATCH_US);
    }

    private void scheduleEmit(long delayMs) {
        if (closed || uploadPending) return;
        uploadPending = true;
        handler.postDelayed(emitRunnable, delayMs);
    }

    @Override public void onSensorChanged(SensorEvent event) {
        long now = System.currentTimeMillis();
        int type = event.sensor.getType();
        if (type == Sensor.TYPE_GRAVITY && event.values.length >= 3) {
            classifier.updateGravity(event.values[0], event.values[1], event.values[2], now);
        } else if (type == Sensor.TYPE_LINEAR_ACCELERATION && event.values.length >= 3) {
            classifier.updateMotion(magnitude(event.values[0], event.values[1], event.values[2]), now);
        } else if (type == Sensor.TYPE_ACCELEROMETER && event.values.length >= 3) {
            for (int i = 0; i < 3; i++) gravityEstimate[i] = 0.85f * gravityEstimate[i] + 0.15f * event.values[i];
            classifier.updateGravity(gravityEstimate[0], gravityEstimate[1], gravityEstimate[2], now);
            if (linearAcceleration == null) {
                classifier.updateMotion(magnitude(
                        event.values[0] - gravityEstimate[0],
                        event.values[1] - gravityEstimate[1],
                        event.values[2] - gravityEstimate[2]), now);
            }
        } else if (type == Sensor.TYPE_LIGHT) {
            classifier.updateLight(event.values[0], now);
        } else if (type == Sensor.TYPE_PROXIMITY) {
            classifier.updateProximity(event.values[0], event.sensor.getMaximumRange(), now);
        }
        if (classifier.shouldEmit(now)) scheduleEmit(COALESCE_MS);
    }

    private static float magnitude(float x, float y, float z) {
        return (float) Math.sqrt(x * x + y * y + z * z);
    }

    @Override public void onAccuracyChanged(Sensor sensor, int accuracy) {}

    public void close() {
        closed = true;
        stopContinuousMotion();
        handler.removeCallbacksAndMessages(null);
    }
}
