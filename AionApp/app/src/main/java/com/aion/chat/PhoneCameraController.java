package com.aion.chat;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.ImageFormat;
import android.graphics.Matrix;
import android.graphics.Rect;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.media.Image;
import android.media.ImageReader;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.SystemClock;
import android.util.Log;
import android.util.Range;
import android.util.Size;

import androidx.annotation.NonNull;
import androidx.core.content.ContextCompat;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

final class PhoneCameraController {
    private static final String TAG = "PhoneCameraController";

    interface CaptureCallback {
        void onSuccess(byte[] jpeg, JSONObject metadata);
        void onFailure(String error);
    }

    private final Context context;
    private final CameraManager cameraManager;
    private final Object lock = new Object();
    private HandlerThread cameraThread;
    private Handler cameraHandler;
    private CameraDevice cameraDevice;
    private CameraCaptureSession captureSession;
    private ImageReader imageReader;
    private final PhoneCameraCaptureGate captureGate = new PhoneCameraCaptureGate();

    PhoneCameraController(Context context) {
        this.context = context.getApplicationContext();
        this.cameraManager =
                (CameraManager) this.context.getSystemService(Context.CAMERA_SERVICE);
    }

    JSONObject getCapabilities() {
        JSONObject result = new JSONObject();
        try {
            CameraChoice front = chooseCamera("front");
            CameraChoice back = chooseCamera("back");
            if (front != null) result.put("front", front.toJson());
            if (back != null) result.put("back", back.toJson());
        } catch (Exception ignored) {
        }
        return result;
    }

    boolean capture(String facing, float requestedZoom, CaptureCallback callback) {
        return capture(facing, requestedZoom, callback, 15_000L);
    }

    boolean capture(
            String facing,
            float requestedZoom,
            CaptureCallback callback,
            long timeoutMs
    ) {
        return capture(facing, requestedZoom, callback, timeoutMs, 0L);
    }

    boolean capture(
            String facing,
            float requestedZoom,
            CaptureCallback callback,
            long timeoutMs,
            long targetElapsedMs
    ) {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA)
                != PackageManager.PERMISSION_GRANTED) {
            callback.onFailure("camera_permission_denied");
            return false;
        }
        long token = captureGate.begin();
        if (token < 0L) {
            callback.onFailure("camera_busy");
            return false;
        }
        synchronized (lock) {
            ensureThread();
            final Handler operationHandler = cameraHandler;
            if (!operationHandler.post(() -> openForCapture(
                    PhoneCameraImagePolicy.normalizeFacing(facing),
                    requestedZoom,
                    callback,
                    token,
                    timeoutMs,
                    targetElapsedMs,
                    operationHandler
            ))) {
                captureGate.complete(token);
                callback.onFailure("camera_thread_unavailable");
                return false;
            }
        }
        return true;
    }

    private void openForCapture(
            String facing,
            float requestedZoom,
            CaptureCallback callback,
            long token,
            long timeoutMs,
            long targetElapsedMs,
            Handler operationHandler
    ) {
        final AtomicBoolean completed = new AtomicBoolean(false);
        if (!captureGate.isCurrent(token)) return;
        try {
            CameraChoice choice = chooseCamera(facing);
            if (choice == null) {
                finishFailure(completed, callback, "camera_not_found", token);
                return;
            }
            Size output = chooseOutputSize(choice.characteristics);
            float appliedZoom = PhoneCameraImagePolicy.clampZoom(
                    requestedZoom, choice.minZoom, choice.maxZoom);
            ImageReader captureReader = ImageReader.newInstance(
                    output.getWidth(), output.getHeight(), ImageFormat.JPEG, 2);
            if (!captureGate.isCurrent(token)) {
                captureReader.close();
                return;
            }
            synchronized (lock) {
                if (!captureGate.isCurrent(token)) {
                    captureReader.close();
                    return;
                }
                imageReader = captureReader;
            }
            captureReader.setOnImageAvailableListener(reader -> {
                Image image = null;
                try {
                    image = reader.acquireLatestImage();
                    if (image == null || completed.get()
                            || !captureGate.isCurrent(token)) return;
                    ByteBuffer buffer = image.getPlanes()[0].getBuffer();
                    byte[] raw = new byte[buffer.remaining()];
                    buffer.get(raw);
                    byte[] processed = normalizeAndCompress(
                            raw, choice.sensorOrientation, "front".equals(facing));
                    JSONObject metadata = new JSONObject();
                    metadata.put("camera_id", choice.cameraId);
                    metadata.put("facing", facing);
                    metadata.put("requested_zoom", requestedZoom);
                    metadata.put("applied_zoom", appliedZoom);
                    Bitmap bounds = BitmapFactory.decodeByteArray(
                            processed, 0, processed.length);
                    if (bounds != null) {
                        metadata.put("width", bounds.getWidth());
                        metadata.put("height", bounds.getHeight());
                        bounds.recycle();
                    }
                    metadata.put("byte_size", processed.length);
                    finishSuccess(completed, callback, processed, metadata, token);
                } catch (Exception error) {
                    finishFailure(completed, callback,
                            "image_processing_failed:" + error.getClass().getSimpleName(),
                            token);
                } finally {
                    if (image != null) image.close();
                }
            }, operationHandler);

            operationHandler.postDelayed(
                    () -> finishFailure(
                            completed, callback, "capture_timeout", token),
                    timeoutMs
            );
            cameraManager.openCamera(
                    choice.cameraId,
                    new CameraDevice.StateCallback() {
                        @Override
                        public void onOpened(@NonNull CameraDevice camera) {
                            synchronized (lock) {
                                if (!captureGate.isCurrent(token)) {
                                    camera.close();
                                    return;
                                }
                                cameraDevice = camera;
                            }
                            configureCaptureSession(
                                    camera,
                                    captureReader,
                                    choice,
                                    appliedZoom,
                                    completed,
                                    callback,
                                    token,
                                    targetElapsedMs,
                                    operationHandler);
                        }

                        @Override
                        public void onDisconnected(@NonNull CameraDevice camera) {
                            camera.close();
                            finishFailure(
                                    completed, callback, "camera_disconnected", token);
                        }

                        @Override
                        public void onError(@NonNull CameraDevice camera, int error) {
                            camera.close();
                            finishFailure(
                                    completed, callback, "camera_error_" + error, token);
                        }
                    },
                    operationHandler
            );
        } catch (SecurityException error) {
            finishFailure(completed, callback, "camera_permission_denied", token);
        } catch (CameraAccessException error) {
            finishFailure(
                    completed, callback, cameraAccessError(error), token);
        } catch (Exception error) {
            finishFailure(completed, callback,
                    "camera_open_failed:" + error.getClass().getSimpleName(),
                    token);
        }
    }

    private void configureCaptureSession(
            CameraDevice camera,
            ImageReader captureReader,
            CameraChoice choice,
            float zoom,
            AtomicBoolean completed,
            CaptureCallback callback,
            long token,
            long targetElapsedMs,
            Handler operationHandler
    ) {
        try {
            camera.createCaptureSession(
                    Collections.singletonList(captureReader.getSurface()),
                    new CameraCaptureSession.StateCallback() {
                        @Override
                        public void onConfigured(
                                @NonNull CameraCaptureSession session
                        ) {
                            if (completed.get() || !captureGate.isCurrent(token)) {
                                session.close();
                                return;
                            }
                            synchronized (lock) {
                                if (!captureGate.isCurrent(token)) {
                                    session.close();
                                    return;
                                }
                                captureSession = session;
                            }
                            long nowElapsedMs = SystemClock.elapsedRealtime();
                            long captureDelayMs = PhoneCameraCaptureTiming.delayMs(
                                    nowElapsedMs, targetElapsedMs);
                            if (targetElapsedMs > 0L) {
                                Log.i(TAG, "camera session ready target="
                                        + targetElapsedMs
                                        + " now=" + nowElapsedMs
                                        + " delayMs=" + captureDelayMs
                                        + " lateByMs="
                                        + Math.max(0L, nowElapsedMs - targetElapsedMs));
                            }
                            operationHandler.postDelayed(() -> issueStillCapture(
                                    camera,
                                    session,
                                    captureReader,
                                    choice,
                                    zoom,
                                    completed,
                                    callback,
                                    token,
                                    targetElapsedMs,
                                    operationHandler
                            ), captureDelayMs);
                        }

                        @Override
                        public void onConfigureFailed(
                                @NonNull CameraCaptureSession session
                        ) {
                            finishFailure(completed, callback,
                                    "capture_session_configuration_failed",
                                    token);
                        }
                    },
                    operationHandler
            );
        } catch (CameraAccessException error) {
            finishFailure(
                    completed, callback, cameraAccessError(error), token);
        }
    }

    private void issueStillCapture(
            CameraDevice camera,
            CameraCaptureSession session,
            ImageReader captureReader,
            CameraChoice choice,
            float zoom,
            AtomicBoolean completed,
            CaptureCallback callback,
            long token,
            long targetElapsedMs,
            Handler operationHandler
    ) {
        if (!captureGate.isCurrent(token)) return;
        try {
            CaptureRequest.Builder builder =
                    camera.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE);
            builder.addTarget(captureReader.getSurface());
            builder.set(CaptureRequest.CONTROL_AF_MODE,
                    CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE);
            builder.set(CaptureRequest.CONTROL_AE_MODE,
                    CaptureRequest.CONTROL_AE_MODE_ON);
            builder.set(CaptureRequest.JPEG_QUALITY, (byte) 92);
            builder.set(CaptureRequest.JPEG_ORIENTATION, 0);
            applyZoom(builder, choice.characteristics, zoom);
            if (targetElapsedMs > 0L) {
                long submittedElapsedMs = SystemClock.elapsedRealtime();
                Log.i(TAG, "still capture submitted target="
                        + targetElapsedMs
                        + " submitted=" + submittedElapsedMs
                        + " deltaMs="
                        + (submittedElapsedMs - targetElapsedMs));
            }
            session.capture(
                    builder.build(),
                    new CameraCaptureSession.CaptureCallback() {},
                    operationHandler
            );
        } catch (CameraAccessException error) {
            finishFailure(
                    completed, callback, cameraAccessError(error), token);
        } catch (Exception error) {
            finishFailure(completed, callback,
                    "still_capture_failed:" + error.getClass().getSimpleName(),
                    token);
        }
    }

    private static void applyZoom(
            CaptureRequest.Builder builder,
            CameraCharacteristics characteristics,
            float zoom
    ) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            Range<Float> range = characteristics.get(
                    CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE);
            if (range != null) {
                builder.set(CaptureRequest.CONTROL_ZOOM_RATIO,
                        PhoneCameraImagePolicy.clampZoom(
                                zoom, range.getLower(), range.getUpper()));
                return;
            }
        }
        Rect active = characteristics.get(
                CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE);
        Float maxDigital = characteristics.get(
                CameraCharacteristics.SCALER_AVAILABLE_MAX_DIGITAL_ZOOM);
        if (active == null || maxDigital == null) return;
        float applied = PhoneCameraImagePolicy.clampZoom(zoom, 1f, maxDigital);
        int cropWidth = Math.round(active.width() / applied);
        int cropHeight = Math.round(active.height() / applied);
        int left = active.centerX() - cropWidth / 2;
        int top = active.centerY() - cropHeight / 2;
        builder.set(CaptureRequest.SCALER_CROP_REGION,
                new Rect(left, top, left + cropWidth, top + cropHeight));
    }

    private static String cameraAccessError(CameraAccessException error) {
        switch (error.getReason()) {
            case CameraAccessException.CAMERA_IN_USE:
                return "camera_access_in_use";
            case CameraAccessException.MAX_CAMERAS_IN_USE:
                return "camera_access_max_in_use";
            case CameraAccessException.CAMERA_DISCONNECTED:
                return "camera_access_disconnected";
            case CameraAccessException.CAMERA_DISABLED:
                return "camera_access_disabled";
            case CameraAccessException.CAMERA_ERROR:
            default:
                return "camera_access_error";
        }
    }

    private CameraChoice chooseCamera(String facing) throws CameraAccessException {
        if (cameraManager == null) return null;
        int wanted = "front".equals(facing)
                ? CameraCharacteristics.LENS_FACING_FRONT
                : CameraCharacteristics.LENS_FACING_BACK;
        CameraChoice best = null;
        for (String cameraId : cameraManager.getCameraIdList()) {
            CameraCharacteristics chars =
                    cameraManager.getCameraCharacteristics(cameraId);
            Integer actual = chars.get(CameraCharacteristics.LENS_FACING);
            if (actual == null || actual != wanted) continue;
            Range<Float> range = Build.VERSION.SDK_INT >= Build.VERSION_CODES.R
                    ? chars.get(CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE)
                    : null;
            float min = range != null ? range.getLower() : 1f;
            Float digital = chars.get(
                    CameraCharacteristics.SCALER_AVAILABLE_MAX_DIGITAL_ZOOM);
            float max = range != null
                    ? range.getUpper()
                    : (digital != null ? digital : 1f);
            Integer orientation = chars.get(
                    CameraCharacteristics.SENSOR_ORIENTATION);
            CameraChoice candidate = new CameraChoice(
                    cameraId, chars, min, max, orientation == null ? 0 : orientation);
            if (best == null
                    || candidate.minZoom < best.minZoom
                    || (candidate.minZoom == best.minZoom
                    && candidate.maxZoom > best.maxZoom)) {
                best = candidate;
            }
        }
        return best;
    }

    private static Size chooseOutputSize(CameraCharacteristics characteristics) {
        android.hardware.camera2.params.StreamConfigurationMap map =
                characteristics.get(
                        CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        if (map == null) return new Size(1280, 720);
        Size[] sizes = map.getOutputSizes(ImageFormat.JPEG);
        if (sizes == null || sizes.length == 0) return new Size(1280, 720);
        return Arrays.stream(sizes)
                .min((left, right) -> {
                    int leftDistance = Math.abs(
                            Math.max(left.getWidth(), left.getHeight())
                                    - PhoneCameraImagePolicy.MAX_LONG_EDGE);
                    int rightDistance = Math.abs(
                            Math.max(right.getWidth(), right.getHeight())
                                    - PhoneCameraImagePolicy.MAX_LONG_EDGE);
                    return Integer.compare(leftDistance, rightDistance);
                })
                .orElse(sizes[0]);
    }

    private static byte[] normalizeAndCompress(
            byte[] raw,
            int rotation,
            boolean mirror
    ) {
        Bitmap source = BitmapFactory.decodeByteArray(raw, 0, raw.length);
        if (source == null) throw new IllegalArgumentException("decode_failed");
        Matrix matrix = new Matrix();
        if (rotation != 0) matrix.postRotate(rotation);
        if (mirror) matrix.postScale(-1f, 1f);
        Bitmap oriented = Bitmap.createBitmap(
                source, 0, 0, source.getWidth(), source.getHeight(), matrix, true);
        if (oriented != source) source.recycle();

        int[] target = PhoneCameraImagePolicy.targetDimensions(
                oriented.getWidth(), oriented.getHeight(),
                PhoneCameraImagePolicy.MAX_LONG_EDGE);
        Bitmap scaled = oriented;
        if (target[0] != oriented.getWidth() || target[1] != oriented.getHeight()) {
            scaled = Bitmap.createScaledBitmap(oriented, target[0], target[1], true);
            if (scaled != oriented) oriented.recycle();
        }

        int quality = PhoneCameraImagePolicy.INITIAL_JPEG_QUALITY;
        byte[] encoded;
        do {
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            scaled.compress(Bitmap.CompressFormat.JPEG, quality, output);
            encoded = output.toByteArray();
            if (encoded.length <= PhoneCameraImagePolicy.MAX_JPEG_BYTES
                    || quality <= PhoneCameraImagePolicy.MIN_JPEG_QUALITY) {
                break;
            }
            quality = PhoneCameraImagePolicy.nextJpegQuality(quality);
        } while (true);
        scaled.recycle();
        return encoded;
    }

    private void finishSuccess(
            AtomicBoolean completed,
            CaptureCallback callback,
            byte[] jpeg,
            JSONObject metadata,
            long token
    ) {
        synchronized (lock) {
            if (!captureGate.isCurrent(token)
                    || !completed.compareAndSet(false, true)) return;
            closeCaptureResources();
            captureGate.complete(token);
        }
        callback.onSuccess(jpeg, metadata);
    }

    private void finishFailure(
            AtomicBoolean completed,
            CaptureCallback callback,
            String error,
            long token
    ) {
        synchronized (lock) {
            if (!captureGate.isCurrent(token)
                    || !completed.compareAndSet(false, true)) return;
            closeCaptureResources();
            captureGate.complete(token);
        }
        callback.onFailure(error);
    }

    private void ensureThread() {
        synchronized (lock) {
            if (cameraThread != null && cameraThread.isAlive()) return;
            cameraThread = new HandlerThread("PhoneCamera");
            cameraThread.start();
            cameraHandler = new Handler(cameraThread.getLooper());
        }
    }

    void close() {
        synchronized (lock) {
            captureGate.cancel();
            closeCaptureResources();
            if (cameraThread != null) {
                cameraThread.quitSafely();
                cameraThread = null;
                cameraHandler = null;
            }
        }
    }

    private void closeCaptureResources() {
        if (captureSession != null) {
            try {
                captureSession.close();
            } catch (Exception ignored) {
            }
            captureSession = null;
        }
        if (cameraDevice != null) {
            try {
                cameraDevice.close();
            } catch (Exception ignored) {
            }
            cameraDevice = null;
        }
        if (imageReader != null) {
            try {
                imageReader.close();
            } catch (Exception ignored) {
            }
            imageReader = null;
        }
    }

    private static final class CameraChoice {
        final String cameraId;
        final CameraCharacteristics characteristics;
        final float minZoom;
        final float maxZoom;
        final int sensorOrientation;

        CameraChoice(
                String cameraId,
                CameraCharacteristics characteristics,
                float minZoom,
                float maxZoom,
                int sensorOrientation
        ) {
            this.cameraId = cameraId;
            this.characteristics = characteristics;
            this.minZoom = minZoom;
            this.maxZoom = maxZoom;
            this.sensorOrientation = sensorOrientation;
        }

        JSONObject toJson() throws Exception {
            JSONObject result = new JSONObject();
            result.put("cameraId", cameraId);
            result.put("minZoom", minZoom);
            result.put("maxZoom", maxZoom);
            JSONArray presets = new JSONArray();
            for (Float value : PhoneCameraImagePolicy.zoomPresets(minZoom, maxZoom)) {
                presets.put(value);
            }
            result.put("presets", presets);
            return result;
        }
    }
}
