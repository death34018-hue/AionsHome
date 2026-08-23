package com.aion.chat;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Parses the small text subset needed from vivo's notification service dump. */
public final class SuperXDumpParser {
    private static final Pattern RECORD = Pattern.compile(
            "(?ms)^\\s*NotificationRecord\\([^\\n]*?pkg=([\\w.]+)[^\\n]*?"
                    + "tag=VIVO_SUPERX_TAG[^\\n]*\\)(.*?)"
                    + "(?=^\\s*NotificationRecord\\(|\\z)");
    private static final Pattern SCENE = Pattern.compile(
            "notification\\.superx\\.scene=String \\(([^\\r\\n)]*)\\)");
    private static final Pattern STANDARD_TITLE = Pattern.compile(
            "android\\.title=String \\(([^\\r\\n)]*)\\)");
    private static final Pattern STANDARD_TEXT = Pattern.compile(
            "android\\.text=String \\(([^\\r\\n)]*)\\)");
    private static final Pattern BASE_TITLE = Pattern.compile(
            "notification\\.superx\\.baseInfos\\.title=([^,}\\]\\r\\n]+)");
    private static final Pattern BASE_CONTENT = Pattern.compile(
            "notification\\.superx\\.baseInfos\\.content=([^,}\\]\\r\\n]+)");

    private SuperXDumpParser() {}

    public static List<Card> parse(String dump) {
        if (dump == null || dump.isEmpty()) return Collections.emptyList();
        List<Card> cards = new ArrayList<>();
        Matcher records = RECORD.matcher(dump);
        while (records.find()) {
            String packageName = clean(records.group(1));
            String body = records.group(2);
            String title = first(body, STANDARD_TITLE, BASE_TITLE);
            String text = first(body, STANDARD_TEXT, BASE_CONTENT);
            if (packageName.isEmpty() || (title.isEmpty() && text.isEmpty())) continue;
            String scene = match(body, SCENE);
            cards.add(new Card(packageName, scene, title, text, isTerminal(title, text)));
        }
        return cards;
    }

    private static String first(String source, Pattern primary, Pattern fallback) {
        String value = match(source, primary);
        return value.isEmpty() ? match(source, fallback) : value;
    }

    private static String match(String source, Pattern pattern) {
        Matcher matcher = pattern.matcher(source);
        return matcher.find() ? clean(matcher.group(1)) : "";
    }

    private static boolean isTerminal(String title, String text) {
        String content = title + " " + text;
        return content.contains("已送达") || content.contains("订单完成")
                || content.contains("已完成") || content.contains("行程结束")
                || content.contains("已到达");
    }

    private static String clean(String value) {
        return value == null ? "" : value.replaceAll("\\s+", " ").trim();
    }

    public static final class Card {
        public final String packageName;
        public final String scene;
        public final String title;
        public final String text;
        public final boolean terminal;

        Card(String packageName, String scene, String title, String text, boolean terminal) {
            this.packageName = packageName;
            this.scene = scene;
            this.title = title;
            this.text = text;
            this.terminal = terminal;
        }
    }
}
