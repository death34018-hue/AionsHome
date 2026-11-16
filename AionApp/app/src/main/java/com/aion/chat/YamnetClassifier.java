package com.aion.chat;

import android.content.Context;
import android.content.res.AssetFileDescriptor;
import org.tensorflow.lite.DataType;
import org.tensorflow.lite.Interpreter;
import java.io.BufferedReader;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.MappedByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/** One worker owns this classifier; PCM stays on the device. */
final class YamnetClassifier implements AutoCloseable {
    static final int SAMPLE_RATE = 16000, WINDOW = 15600, HOP = 7800;
    private final Interpreter interpreter;
    private final MappedByteBuffer model;
    private final ByteBuffer input = ByteBuffer.allocateDirect(WINDOW * 4).order(ByteOrder.nativeOrder());
    private final ByteBuffer output = ByteBuffer.allocateDirect(521 * 4).order(ByteOrder.nativeOrder());
    final List<String> labels = new ArrayList<>();

    YamnetClassifier(Context context) throws IOException {
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(
                context.getAssets().open("sound/labels.txt"), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) labels.add(line);
        }
        try (AssetFileDescriptor asset = context.getAssets().openFd("sound/yamnet.tflite");
             FileInputStream stream = new FileInputStream(asset.getFileDescriptor())) {
            model = stream.getChannel().map(FileChannel.MapMode.READ_ONLY,
                    asset.getStartOffset(), asset.getDeclaredLength());
        }
        interpreter = new Interpreter(model, new Interpreter.Options().setNumThreads(2));
        if (labels.size() != 521 || interpreter.getInputTensorCount() != 1
                || interpreter.getOutputTensorCount() != 1
                || interpreter.getInputTensor(0).numElements() != WINDOW
                || interpreter.getOutputTensor(0).numElements() != 521
                || interpreter.getInputTensor(0).dataType() != DataType.FLOAT32
                || interpreter.getOutputTensor(0).dataType() != DataType.FLOAT32) {
            interpreter.close();
            throw new IOException("声音模型格式不匹配");
        }
    }

    float[] classify(short[] pcm) {
        if (pcm.length != WINDOW) throw new IllegalArgumentException("Expected 975 ms of audio");
        input.rewind();
        for (short sample : pcm) input.putFloat(sample / 32768f);
        input.rewind();
        output.rewind();
        interpreter.run(input, output);
        output.rewind();
        float[] scores = new float[521];
        output.asFloatBuffer().get(scores);
        return scores;
    }

    String label(int index) {
        String name = labels.get(index);
        switch (name) {
            case "Speech": return "说话";
            case "Child speech, kid speaking": return "儿童说话";
            case "Conversation": return "交谈";
            case "Narration, monologue": return "独白";
            case "Whispering": return "耳语";
            case "Singing": return "唱歌";
            case "Child singing": return "儿童唱歌";
            case "Choir": return "合唱";
            case "Humming": return "哼唱";
            case "Snoring": return "鼾声";
            case "Breathing": return "呼吸";
            case "Music": return "音乐";
            case "Silence": return "安静";
            case "Laughter": return "笑声";
            case "Cough": return "咳嗽";
            case "Television": return "电视声";
            case "Inside, small room": return "室内环境声";
            case "Noise": return "噪声";
            case "White noise": return "白噪声";
            case "Wind": return "风声";
            case "Water": return "水声";
            default: return name;
        }
    }

    @Override public void close() { interpreter.close(); }
}
