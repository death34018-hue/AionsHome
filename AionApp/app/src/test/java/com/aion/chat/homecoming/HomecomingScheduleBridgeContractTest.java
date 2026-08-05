package com.aion.chat.homecoming;

import org.junit.Test;

import java.lang.reflect.Method;
import java.util.Arrays;

import static org.junit.Assert.assertEquals;

public class HomecomingScheduleBridgeContractTest {
    @Test
    public void scheduleBridgeAcceptsOnlyTypedLocalFields() throws Exception {
        Method list = HomecomingBridge.class.getDeclaredMethod("listSchedules");
        Method create = HomecomingBridge.class.getDeclaredMethod(
                "createSchedule",
                String.class,
                long.class,
                String.class,
                String.class,
                String.class);
        Method delete = HomecomingBridge.class.getDeclaredMethod(
                "deleteSchedule", String.class);

        assertEquals(String.class, list.getReturnType());
        assertEquals(String.class, create.getReturnType());
        assertEquals(boolean.class, delete.getReturnType());
        assertEquals(
                Arrays.asList(
                        String.class, long.class, String.class,
                        String.class, String.class),
                Arrays.asList(create.getParameterTypes()));
    }
}
