package com.aion.chat.homecoming;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public final class HomecomingControlParser {
    private static final Pattern ALLOWED = Pattern.compile(
            "\\[(ALARM|REMINDER|MONITOR|SCHEDULE_DEL)\\s*:\\s*([^\\]]*)\\]"
                    + "|\\[SCHEDULE_LIST\\]"
                    + "|\\[CAM_CHECK\\]"
                    + "|\\[APP_(LOCK|TEMP_UNLOCK|UNLOCK)\\s*:\\s*([^\\]]*)\\]",
            Pattern.CASE_INSENSITIVE);

    private HomecomingControlParser() {
    }

    public static Result parse(String text) {
        String source = text == null ? "" : text;
        Matcher matcher = ALLOWED.matcher(source);
        ArrayList<ControlEvent> events = new ArrayList<>();
        StringBuffer visible = new StringBuffer();
        while (matcher.find()) {
            String full = matcher.group();
            String upper = full.toUpperCase(Locale.ROOT);
            String type;
            if (upper.startsWith("[ALARM:")) type = "alarm";
            else if (upper.startsWith("[REMINDER:")) type = "reminder";
            else if (upper.startsWith("[MONITOR:")) type = "monitor";
            else if (upper.startsWith("[SCHEDULE_DEL:")) type = "schedule_delete";
            else if (upper.startsWith("[SCHEDULE_LIST")) type = "schedule_list";
            else if (upper.startsWith("[CAM_CHECK")) type = "camera_check";
            else type = "app_supervision";
            List<String> arguments = arguments(type, full);
            if (arguments == null) {
                matcher.appendReplacement(visible, Matcher.quoteReplacement(full));
                continue;
            }
            events.add(new ControlEvent(type, full, arguments));
            matcher.appendReplacement(visible, "");
        }
        matcher.appendTail(visible);
        return new Result(visible.toString().trim(), events);
    }

    private static List<String> arguments(String type, String tag) {
        if ("camera_check".equals(type) || "schedule_list".equals(type)) {
            return Collections.emptyList();
        }
        int colon = tag.indexOf(':');
        int end = tag.lastIndexOf(']');
        if (colon < 0 || end <= colon + 1) return null;
        String body = tag.substring(colon + 1, end).trim();
        ArrayList<String> values = new ArrayList<>();
        if ("alarm".equals(type) || "reminder".equals(type)
                || "monitor".equals(type)) {
            int separator = body.indexOf('|');
            if (separator <= 0 || separator >= body.length() - 1) return null;
            values.add(body.substring(0, separator).trim());
            values.add(body.substring(separator + 1).trim());
        } else if ("schedule_delete".equals(type)) {
            if (body.contains("|")) return null;
            values.add(body);
        } else {
            String[] parts = body.split("\\|", -1);
            for (String part : parts) values.add(part.trim());
        }
        for (String value : values) {
            if (value.isEmpty()) return null;
        }
        return Collections.unmodifiableList(values);
    }

    public static final class Result {
        public final String visibleText;
        public final List<ControlEvent> events;
        Result(String visibleText, List<ControlEvent> events) {
            this.visibleText = visibleText;
            this.events = Collections.unmodifiableList(new ArrayList<>(events));
        }
        public List<String> types() {
            ArrayList<String> values = new ArrayList<>();
            for (ControlEvent event : events) values.add(event.type);
            return values;
        }
    }

    public static final class ControlEvent {
        public final String type;
        public final String rawTag;
        public final List<String> arguments;
        ControlEvent(String type, String rawTag, List<String> arguments) {
            this.type = type;
            this.rawTag = rawTag;
            this.arguments = Collections.unmodifiableList(new ArrayList<>(arguments));
        }
    }
}
