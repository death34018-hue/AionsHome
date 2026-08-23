package com.aion.chat.homecoming;

import java.util.Locale;

final class HomecomingModelStreamNormalizer {
    private static final String OPEN_TAG = "<think>";
    private static final String CLOSE_TAG = "</think>";

    private final StringBuilder pending = new StringBuilder();
    private boolean reasoning;

    String acceptVisible(String chunk) {
        if (chunk != null && !chunk.isEmpty()) {
            pending.append(chunk);
        }
        StringBuilder visible = new StringBuilder();
        while (pending.length() > 0) {
            String tag = reasoning ? CLOSE_TAG : OPEN_TAG;
            int tagIndex = indexOfIgnoreCase(pending, tag);
            if (tagIndex >= 0) {
                if (!reasoning && tagIndex > 0) {
                    visible.append(pending, 0, tagIndex);
                }
                pending.delete(0, tagIndex + tag.length());
                reasoning = !reasoning;
                continue;
            }

            int retained = matchingSuffixLength(pending, tag);
            int confirmed = pending.length() - retained;
            if (!reasoning && confirmed > 0) {
                visible.append(pending, 0, confirmed);
            }
            pending.delete(0, confirmed);
            break;
        }
        return visible.toString();
    }

    String finish() {
        if (reasoning) {
            pending.setLength(0);
            return "";
        }
        String visible = pending.toString();
        pending.setLength(0);
        return visible;
    }

    private static int indexOfIgnoreCase(CharSequence value, String target) {
        String source = value.toString().toLowerCase(Locale.ROOT);
        return source.indexOf(target);
    }

    private static int matchingSuffixLength(CharSequence value, String target) {
        int maximum = Math.min(value.length(), target.length() - 1);
        for (int length = maximum; length > 0; length--) {
            int offset = value.length() - length;
            if (value.toString().regionMatches(
                    true, offset, target, 0, length)) {
                return length;
            }
        }
        return 0;
    }
}
