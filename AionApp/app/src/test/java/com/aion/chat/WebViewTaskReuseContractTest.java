package com.aion.chat;

import org.junit.Test;
import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;

import java.nio.file.Paths;

import javax.xml.parsers.DocumentBuilderFactory;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

public class WebViewTaskReuseContractTest {
    private static final String ANDROID_NAMESPACE =
            "http://schemas.android.com/apk/res/android";

    @Test
    public void launcherRelaunchReusesTheExistingWebViewActivity() throws Exception {
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        factory.setNamespaceAware(true);
        Document document = factory.newDocumentBuilder().parse(
                Paths.get("src/main/AndroidManifest.xml").toFile());
        NodeList activities = document.getElementsByTagName("activity");

        for (int index = 0; index < activities.getLength(); index++) {
            Element activity = (Element) activities.item(index);
            String name = activity.getAttributeNS(ANDROID_NAMESPACE, "name");
            if (".WebViewActivity".equals(name)) {
                assertEquals(
                        "Repeated launcher or ADB starts must reuse the current WebView",
                        "singleTask",
                        activity.getAttributeNS(ANDROID_NAMESPACE, "launchMode"));
                assertTrue(
                        "The desktop icon must target the singleTask WebView directly",
                        hasLauncherIntentFilter(activity));
                return;
            }
        }
        fail("WebViewActivity is missing from AndroidManifest.xml");
    }

    @Test
    public void addressPickerIsNotTheDesktopEntryPoint() throws Exception {
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        factory.setNamespaceAware(true);
        Document document = factory.newDocumentBuilder().parse(
                Paths.get("src/main/AndroidManifest.xml").toFile());
        NodeList activities = document.getElementsByTagName("activity");

        for (int index = 0; index < activities.getLength(); index++) {
            Element activity = (Element) activities.item(index);
            String name = activity.getAttributeNS(ANDROID_NAMESPACE, "name");
            if (".LauncherActivity".equals(name)) {
                assertFalse(
                        "The transient address picker must not own the desktop icon",
                        hasLauncherIntentFilter(activity));
                return;
            }
        }
        fail("LauncherActivity is missing from AndroidManifest.xml");
    }

    private static boolean hasLauncherIntentFilter(Element activity) {
        NodeList actions = activity.getElementsByTagName("action");
        NodeList categories = activity.getElementsByTagName("category");
        boolean hasMain = hasAndroidName(actions, "android.intent.action.MAIN");
        boolean hasLauncher = hasAndroidName(
                categories, "android.intent.category.LAUNCHER");
        return hasMain && hasLauncher;
    }

    private static boolean hasAndroidName(NodeList nodes, String expected) {
        for (int index = 0; index < nodes.getLength(); index++) {
            Node node = nodes.item(index);
            if (node instanceof Element
                    && expected.equals(((Element) node).getAttributeNS(
                    ANDROID_NAMESPACE, "name"))) {
                return true;
            }
        }
        return false;
    }
}
