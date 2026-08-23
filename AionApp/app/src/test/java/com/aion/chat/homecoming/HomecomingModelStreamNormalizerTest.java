package com.aion.chat.homecoming;

import org.junit.Test;

import static org.junit.Assert.assertEquals;

public class HomecomingModelStreamNormalizerTest {
    @Test
    public void splitThinkTagsNeverReachVisibleText() {
        HomecomingModelStreamNormalizer normalizer =
                new HomecomingModelStreamNormalizer();

        assertEquals("前", normalizer.acceptVisible("前<thi"));
        assertEquals("", normalizer.acceptVisible("nk>秘密</th"));
        assertEquals("后", normalizer.acceptVisible("ink>后"));
        assertEquals("", normalizer.finish());
    }

    @Test
    public void completeThinkTagsAreRemovedCaseInsensitively() {
        HomecomingModelStreamNormalizer normalizer =
                new HomecomingModelStreamNormalizer();

        assertEquals("前后", normalizer.acceptVisible(
                "前<THINK>不能显示</ThInK>后"));
        assertEquals("", normalizer.finish());
    }

    @Test
    public void unclosedThinkRegionIsDiscardedAtEnd() {
        HomecomingModelStreamNormalizer normalizer =
                new HomecomingModelStreamNormalizer();

        assertEquals("正文", normalizer.acceptVisible("正文<think>秘密"));
        assertEquals("", normalizer.finish());
    }

    @Test
    public void ordinaryAngleBracketsRemainVisible() {
        HomecomingModelStreamNormalizer normalizer =
                new HomecomingModelStreamNormalizer();

        assertEquals("1 < 2，正文", normalizer.acceptVisible("1 < 2，正文"));
        assertEquals("", normalizer.finish());
    }
}
