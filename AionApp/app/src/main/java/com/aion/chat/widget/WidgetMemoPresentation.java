package com.aion.chat.widget;

import java.util.List;

final class WidgetMemoPresentation {
    final boolean emptyHintVisible;
    final boolean firstVisible;
    final boolean secondVisible;
    final String firstText;
    final String secondText;

    private WidgetMemoPresentation(boolean emptyHintVisible,
                                   boolean firstVisible, boolean secondVisible,
                                   String firstText, String secondText) {
        this.emptyHintVisible = emptyHintVisible;
        this.firstVisible = firstVisible;
        this.secondVisible = secondVisible;
        this.firstText = firstText;
        this.secondText = secondText;
    }

    static WidgetMemoPresentation from(List<PrivateMemo> memos) {
        int count = memos == null ? 0 : memos.size();
        return new WidgetMemoPresentation(
                count == 0,
                count > 0,
                count > 1,
                count > 0 ? memos.get(0).content : "",
                count > 1 ? memos.get(1).content : ""
        );
    }
}
