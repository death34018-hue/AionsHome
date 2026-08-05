package com.aion.chat;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.ImageFormat;
import android.graphics.Matrix;
import android.graphics.Rect;
import android.graphics.YuvImage;
import android.hardware.Camera;
import android.util.Base64;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import java.io.ByteArrayOutputStream;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 原生摄像头桥接 — 绕过 WebView getUserMedia 的 HTTPS 限制
 *
 * 优化要点：
 * 1. setPreviewCallbackWithBuffer 避免每帧 GC
 * 2. NV21 纯字节数组旋转（不经过 Bitmap），Java 端输出已旋转的竖屏 JPEG
 * 3. 后台线程编码，不阻塞摄像头回调
 * 4. capture() 仅截图时调用，用高质量 JPEG
 *
 * 前端调用: window.AionCamera.start/stop/flip/getFrame/capture/isRunning/getFacing
 * 前端无需做 CSS rotation — Java 端已旋转好
 */
@SuppressWarnings("deprecation")
public class CameraBridge {

    private final WebView webView;
    private Camera camera;
    private final CameraPreviewState previewState = new CameraPreviewState();
    private final CameraPreviewSessionGate sessionGate =
            new CameraPreviewSessionGate();
    private int facing;
    private int sensorRotation;
    private int previewWidth, previewHeight;
    private volatile String lastFrameB64 = null;
    private volatile byte[] lastRotatedNv21 = null; // 旋转后的 NV21（用于截图）
    private int rotatedWidth, rotatedHeight;         // 旋转后的尺寸
    private static final long FRAME_INTERVAL_MS = 66; // ~15fps

    private final ExecutorService encodeThread = Executors.newSingleThreadExecutor();

    // 可复用缓冲区
    private byte[] rotateBuf;
    private byte[] inputBuf;  // 专用输入缓冲区，不与 lastRotatedNv21 共用
    private android.graphics.SurfaceTexture surfaceTexture; // 保持引用以便释放

    // 视频录制桥接 — 录制时同步推送帧
    private volatile VideoBridge videoBridge;

    public CameraBridge(WebView webView) {
        this.webView = webView;
    }

    /** 设置视频录制桥接，录制时自动推送预览帧 */
    public void setVideoBridge(VideoBridge vb) {
        this.videoBridge = vb;
    }

    /** 返回当前旋转后的画面尺寸 [width, height]，供 VideoBridge 初始化编码器 */
    @JavascriptInterface
    public int getRotatedWidth() { return rotatedWidth; }

    @JavascriptInterface
    public int getRotatedHeight() { return rotatedHeight; }

    @JavascriptInterface
    public synchronized boolean start(String facingStr) {
        if (!previewState.canStart()
                || PhoneCameraPreviewCoordinator.shared().isEventActive()) return false;
        if (previewState.isRunning()) stop();

        facing = "user".equals(facingStr)
                ? Camera.CameraInfo.CAMERA_FACING_FRONT
                : Camera.CameraInfo.CAMERA_FACING_BACK;

        int camId = findCameraId(facing);
        if (camId < 0) return false;

        try {
            camera = Camera.open(camId);
        } catch (Exception e) {
            android.util.Log.e("CameraBridge", "Camera.open failed", e);
            previewState.failed();
            return false;
        }
        final long sessionToken = sessionGate.start();
        camera.setErrorCallback((error, failedCamera) ->
                handleCameraError(error, failedCamera, sessionToken));

        Camera.CameraInfo info = new Camera.CameraInfo();
        Camera.getCameraInfo(camId, info);
        sensorRotation = info.orientation;

        Camera.Parameters params = camera.getParameters();

        // 选择接近 640×480 的预览尺寸（清晰度和性能平衡）
        Camera.Size best = null;
        int target = 640 * 480;
        for (Camera.Size s : params.getSupportedPreviewSizes()) {
            if (best == null
                    || Math.abs(s.width * s.height - target) < Math.abs(best.width * best.height - target))
                best = s;
        }
        previewWidth = best != null ? best.width : 640;
        previewHeight = best != null ? best.height : 480;
        params.setPreviewSize(previewWidth, previewHeight);
        params.setPreviewFormat(ImageFormat.NV21);

        // 连续自动对焦
        for (String mode : params.getSupportedFocusModes()) {
            if (Camera.Parameters.FOCUS_MODE_CONTINUOUS_VIDEO.equals(mode)) {
                params.setFocusMode(mode);
                break;
            }
        }

        try { camera.setParameters(params); }
        catch (Exception e) { android.util.Log.w("CameraBridge", "setParameters fail", e); }

        // 计算旋转后尺寸
        if (sensorRotation == 90 || sensorRotation == 270) {
            rotatedWidth = previewHeight;
            rotatedHeight = previewWidth;
        } else {
            rotatedWidth = previewWidth;
            rotatedHeight = previewHeight;
        }

        // 分配可复用缓冲区
        int bufSize = previewWidth * previewHeight * 3 / 2;
        rotateBuf = new byte[bufSize];
        inputBuf = new byte[bufSize];
        final byte[] sessionRotateBuf = rotateBuf;
        final byte[] sessionInputBuf = inputBuf;
        final byte[] sessionOutputBuf = new byte[bufSize];
        final int sessionPreviewWidth = previewWidth;
        final int sessionPreviewHeight = previewHeight;
        final int sessionRotation = sensorRotation;
        final int sessionRotatedWidth = rotatedWidth;
        final int sessionRotatedHeight = rotatedHeight;
        final AtomicBoolean sessionEncoding = new AtomicBoolean(false);

        // 使用 buffer 回调避免 GC 导致的冻结
        camera.addCallbackBuffer(new byte[bufSize]);
        camera.addCallbackBuffer(new byte[bufSize]);
        camera.addCallbackBuffer(new byte[bufSize]);

        camera.setPreviewCallbackWithBuffer((data, cam) -> {
            if (!sessionGate.isCurrent(sessionToken)
                    || !previewState.isRunning() || data == null) {
                if (data != null && cam != null) cam.addCallbackBuffer(data);
                return;
            }

            long now = System.currentTimeMillis();
            long lastFrameAt = previewState.getLastFrameAt();
            if (now - lastFrameAt < FRAME_INTERVAL_MS
                    || !sessionEncoding.compareAndSet(false, true)) {
                cam.addCallbackBuffer(data);
                return;
            }
            previewState.receivedFrame(now);

            // 复制数据到专用输入缓冲区后立即归还 camera buffer
            System.arraycopy(data, 0, sessionInputBuf, 0, data.length);
            cam.addCallbackBuffer(data);

            // 后台线程做旋转 + JPEG 编码
            encodeThread.execute(() -> {
                try {
                    processFrame(
                            sessionInputBuf,
                            sessionRotateBuf,
                            sessionOutputBuf,
                            sessionPreviewWidth,
                            sessionPreviewHeight,
                            sessionRotation,
                            sessionRotatedWidth,
                            sessionRotatedHeight,
                            sessionToken);
                } finally {
                    sessionEncoding.set(false);
                }
            });
        });

        try {
            // 释放旧的 SurfaceTexture
            if (surfaceTexture != null) {
                surfaceTexture.release();
            }
            surfaceTexture = new android.graphics.SurfaceTexture(0);
            surfaceTexture.setDefaultBufferSize(1, 1); // 最小化内部缓冲区防止积压卡死
            camera.setPreviewTexture(surfaceTexture);
            previewState.started();
            camera.startPreview();
        } catch (Exception e) {
            android.util.Log.e("CameraBridge", "startPreview failed", e);
            camera.release();
            camera = null;
            previewState.failed();
            sessionGate.stop();
            return false;
        }

        android.util.Log.i("CameraBridge", "Started: " + facingStr + " " +
                previewWidth + "x" + previewHeight + " rot=" + sensorRotation);
        return true;
    }

    @JavascriptInterface
    public synchronized void stop() {
        sessionGate.stop();
        previewState.stopped();
        if (camera != null) {
            try { camera.setPreviewCallbackWithBuffer(null); } catch (Exception ignored) {}
            try { camera.stopPreview(); } catch (Exception ignored) {}
            try { camera.release(); } catch (Exception ignored) {}
            camera = null;
        }
        if (surfaceTexture != null) {
            try { surfaceTexture.release(); } catch (Exception ignored) {}
            surfaceTexture = null;
        }
        lastFrameB64 = null;
        lastRotatedNv21 = null;
    }

    @JavascriptInterface
    public boolean flip() {
        return start(facing == Camera.CameraInfo.CAMERA_FACING_FRONT ? "environment" : "user");
    }

    /** 返回最近一帧的 base64 JPEG（已旋转为竖屏，前端直接显示） */
    @JavascriptInterface
    public String getFrame() {
        return lastFrameB64;
    }

    /** 截图：高质量 JPEG（已旋转，前置已镜像翻转） */
    @JavascriptInterface
    public String capture() {
        byte[] nv21 = lastRotatedNv21;
        if (nv21 == null) return null;
        try {
            // 用旋转后的数据直接压 JPEG（高质量）
            YuvImage yuv = new YuvImage(nv21, ImageFormat.NV21, rotatedWidth, rotatedHeight, null);
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            yuv.compressToJpeg(new Rect(0, 0, rotatedWidth, rotatedHeight), 40, buf);

            // 前置摄像头需要水平镜像
            if (facing == Camera.CameraInfo.CAMERA_FACING_FRONT) {
                byte[] jpeg = buf.toByteArray();
                Bitmap bmp = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.length);
                if (bmp != null) {
                    Matrix m = new Matrix();
                    m.postScale(-1, 1);
                    Bitmap mirrored = Bitmap.createBitmap(bmp, 0, 0, bmp.getWidth(), bmp.getHeight(), m, false);
                    buf.reset();
                    mirrored.compress(Bitmap.CompressFormat.JPEG, 40, buf);
                    bmp.recycle();
                    if (mirrored != bmp) mirrored.recycle();
                }
            }

            return Base64.encodeToString(buf.toByteArray(), Base64.NO_WRAP);
        } catch (Exception e) {
            return null;
        }
    }

    /** 返回 0 — 旋转已在 Java 端完成，前端无需 CSS rotation */
    @JavascriptInterface
    public int getRotation() { return 0; }

    @JavascriptInterface
    public boolean isRunning() { return previewState.isRunning(); }

    @JavascriptInterface
    public long getLastFrameAt() { return previewState.getLastFrameAt(); }

    void setAppForeground(boolean foreground) {
        previewState.setAppForeground(foreground);
        if (!foreground) stop();
    }

    @JavascriptInterface
    public String getFacing() {
        return facing == Camera.CameraInfo.CAMERA_FACING_FRONT ? "user" : "environment";
    }

    @JavascriptInterface
    public float setZoom(float requestedZoom) {
        Camera active = camera;
        if (active == null) return 1f;
        try {
            Camera.Parameters parameters = active.getParameters();
            if (!parameters.isZoomSupported()) return 1f;
            java.util.List<Integer> ratios = parameters.getZoomRatios();
            int targetRatio = Math.max(100, Math.round(requestedZoom * 100f));
            int bestIndex = 0;
            int bestDistance = Integer.MAX_VALUE;
            for (int index = 0; index < ratios.size(); index++) {
                int distance = Math.abs(ratios.get(index) - targetRatio);
                if (distance < bestDistance) {
                    bestDistance = distance;
                    bestIndex = index;
                }
            }
            parameters.setZoom(bestIndex);
            active.setParameters(parameters);
            return ratios.get(bestIndex) / 100f;
        } catch (Exception error) {
            android.util.Log.w("CameraBridge", "setZoom failed", error);
            return 1f;
        }
    }

    // ── 帧处理：NV21 字节旋转 + JPEG 编码（在后台线程执行） ──

    private void processFrame(
            byte[] nv21Data,
            byte[] sessionRotateBuf,
            byte[] sessionOutputBuf,
            int sessionPreviewWidth,
            int sessionPreviewHeight,
            int sessionRotation,
            int sessionRotatedWidth,
            int sessionRotatedHeight,
            long sessionToken
    ) {
        byte[] src = nv21Data;

        // NV21 纯字节数组旋转（无 Bitmap，极快）
        if (sessionRotation == 90) {
            rotateNV21_CW90(
                    nv21Data, sessionRotateBuf,
                    sessionPreviewWidth, sessionPreviewHeight);
            src = sessionRotateBuf;
        } else if (sessionRotation == 270) {
            rotateNV21_CW270(
                    nv21Data, sessionRotateBuf,
                    sessionPreviewWidth, sessionPreviewHeight);
            src = sessionRotateBuf;
        } else if (sessionRotation == 180) {
            rotateNV21_180(
                    nv21Data, sessionRotateBuf,
                    sessionPreviewWidth, sessionPreviewHeight);
            src = sessionRotateBuf;
        }

        if (!sessionGate.isCurrent(sessionToken)) return;
        System.arraycopy(src, 0, sessionOutputBuf, 0, src.length);

        // 推送给 VideoBridge（录制时）
        VideoBridge vb = videoBridge;
        if (vb != null && vb.isRecording()) {
            vb.onVideoFrame(src, sessionRotatedWidth, sessionRotatedHeight);
        }

        // JPEG 编码（一次编码，已旋转）
        YuvImage yuv = new YuvImage(
                src,
                ImageFormat.NV21,
                sessionRotatedWidth,
                sessionRotatedHeight,
                null);
        ByteArrayOutputStream buf = new ByteArrayOutputStream(16384);
        yuv.compressToJpeg(
                new Rect(0, 0, sessionRotatedWidth, sessionRotatedHeight),
                70,
                buf);
        synchronized (this) {
            if (!sessionGate.isCurrent(sessionToken)
                    || !previewState.isRunning()) return;
            lastRotatedNv21 = sessionOutputBuf;
            lastFrameB64 = Base64.encodeToString(buf.toByteArray(), Base64.NO_WRAP);
        }
    }

    private synchronized void handleCameraError(
            int error,
            Camera failedCamera,
            long sessionToken
    ) {
        android.util.Log.w("CameraBridge", "Camera error/eviction: " + error);
        if (!sessionGate.isCurrent(sessionToken) || camera != failedCamera) {
            try { failedCamera.release(); } catch (Exception ignored) {}
            return;
        }
        sessionGate.stop();
        previewState.failed();
        try { camera.setPreviewCallbackWithBuffer(null); } catch (Exception ignored) {}
        try { camera.stopPreview(); } catch (Exception ignored) {}
        try { camera.release(); } catch (Exception ignored) {}
        camera = null;
        if (surfaceTexture != null) {
            try { surfaceTexture.release(); } catch (Exception ignored) {}
            surfaceTexture = null;
        }
        lastFrameB64 = null;
        lastRotatedNv21 = null;
    }

    // ── NV21 纯字节旋转算法（不涉及 Bitmap，只做数组索引映射） ──

    /** 顺时针 90°：(w,h) → (h,w) */
    private static void rotateNV21_CW90(byte[] in, byte[] out, int w, int h) {
        int i = 0;
        // Y 平面
        for (int x = 0; x < w; x++)
            for (int y = h - 1; y >= 0; y--)
                out[i++] = in[y * w + x];
        // VU 平面（NV21：每 2 字节一组 VU）
        int uvStart = w * h;
        for (int x = 0; x < w; x += 2)
            for (int y = h / 2 - 1; y >= 0; y--) {
                out[i++] = in[uvStart + y * w + x];
                out[i++] = in[uvStart + y * w + x + 1];
            }
    }

    /** 顺时针 270°（逆时针 90°）：(w,h) → (h,w) */
    private static void rotateNV21_CW270(byte[] in, byte[] out, int w, int h) {
        int i = 0;
        for (int x = w - 1; x >= 0; x--)
            for (int y = 0; y < h; y++)
                out[i++] = in[y * w + x];
        int uvStart = w * h;
        for (int x = w - 2; x >= 0; x -= 2)
            for (int y = 0; y < h / 2; y++) {
                out[i++] = in[uvStart + y * w + x];
                out[i++] = in[uvStart + y * w + x + 1];
            }
    }

    /** 180°：(w,h) → (w,h) */
    private static void rotateNV21_180(byte[] in, byte[] out, int w, int h) {
        int ySize = w * h;
        for (int i = 0; i < ySize; i++)
            out[i] = in[ySize - 1 - i];
        int uvSize = ySize / 2;
        for (int i = 0; i < uvSize; i += 2) {
            out[ySize + i]     = in[ySize + uvSize - 2 - i];
            out[ySize + i + 1] = in[ySize + uvSize - 1 - i];
        }
    }

    private int findCameraId(int targetFacing) {
        for (int i = 0; i < Camera.getNumberOfCameras(); i++) {
            Camera.CameraInfo info = new Camera.CameraInfo();
            Camera.getCameraInfo(i, info);
            if (info.facing == targetFacing) return i;
        }
        return -1;
    }
}
