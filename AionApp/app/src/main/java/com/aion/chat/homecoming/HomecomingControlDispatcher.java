package com.aion.chat.homecoming;

import java.util.List;

public final class HomecomingControlDispatcher
        implements HomecomingChatEngine.ControlPort {
    private final SchedulePort schedules;
    private final SupervisionPort supervision;
    private final ResultPort results;
    private final NamePort names;

    HomecomingControlDispatcher(
            SchedulePort schedules,
            SupervisionPort supervision,
            ResultPort results,
            NamePort names) {
        if (schedules == null || supervision == null
                || results == null || names == null) {
            throw new IllegalArgumentException("dispatcher collaborators are required");
        }
        this.schedules = schedules;
        this.supervision = supervision;
        this.results = results;
        this.names = names;
    }

    @Override
    public void apply(
            HomecomingChatEngine.ChatCommand command,
            List<HomecomingControlParser.ControlEvent> controls,
            long now) {
        apply(
                command.requestId,
                command.timelineId,
                command.responderOwner,
                controls,
                now);
    }

    public void apply(
            String requestId,
            String timelineId,
            String responderOwner,
            List<HomecomingControlParser.ControlEvent> controls,
            long now) {
        for (int index = 0; index < controls.size(); index++) {
            HomecomingControlParser.ControlEvent control = controls.get(index);
            if (isSchedule(control.type)) {
                schedules.apply(
                        requestId,
                        index,
                        control,
                        responderOwner,
                        timelineId,
                        now);
                continue;
            }
            if (!"app_supervision".equals(control.type)) {
                results.defer(requestId, control, now);
                continue;
            }
            HomecomingSupervisionCommandHandler.ApplyResult supervisionResult =
                    supervision.apply(
                            requestId,
                            index,
                            control,
                            responderOwner,
                            now);
            if ("deferred".equals(supervisionResult.status)) {
                results.defer(requestId, control, now);
                continue;
            }
            if ("invalid".equals(supervisionResult.status)) continue;
            String actor = configuredName(
                    names.resolve(timelineId, responderOwner));
            String resultText = resultText(actor, supervisionResult);
            String resultRequestId = supervisionResult.commandId.isEmpty()
                    ? requestId + ":supervision:" + index
                    : supervisionResult.commandId;
            results.system(
                    resultRequestId,
                    timelineId,
                    resultText,
                    now);
        }
    }

    private static boolean isSchedule(String type) {
        return "alarm".equals(type)
                || "reminder".equals(type)
                || "monitor".equals(type)
                || "schedule_list".equals(type)
                || "schedule_delete".equals(type);
    }

    private static String resultText(
            String actor,
            HomecomingSupervisionCommandHandler.ApplyResult result) {
        String group = result.groupDisplayName.isEmpty()
                ? "所选应用" : result.groupDisplayName;
        if (!result.applied) {
            return "【" + actor + "】监督指令未执行："
                    + failureText(result.status);
        }
        if ("lock".equals(result.action)) {
            return "【" + actor + "】已锁定" + group
                    + " " + result.minutes + " 分钟";
        }
        if ("temp_unlock".equals(result.action)) {
            return "【" + actor + "】已暂时解锁" + group
                    + " " + result.minutes + " 分钟";
        }
        return "【" + actor + "】已解除" + group + "的锁定";
    }

    private static String failureText(String status) {
        if ("feature_disabled".equals(status)) return "监督功能未启用";
        if ("unknown_group".equals(status)) return "应用组不存在";
        if ("expired".equals(status)) return "指令已经过期";
        return "手机本地执行失败";
    }

    private static String configuredName(String value) {
        return value == null || value.trim().isEmpty() ? "AI" : value.trim();
    }

    interface SchedulePort {
        HomecomingScheduleCommandHandler.ApplyResult apply(
                String requestId,
                int index,
                HomecomingControlParser.ControlEvent event,
                String ownerId,
                String timelineId,
                long now);
    }

    interface SupervisionPort {
        HomecomingSupervisionCommandHandler.ApplyResult apply(
                String requestId,
                int index,
                HomecomingControlParser.ControlEvent event,
                String ownerId,
                long now);
    }

    interface ResultPort {
        void defer(
                String requestId,
                HomecomingControlParser.ControlEvent event,
                long now);
        void system(
                String resultRequestId,
                String timelineId,
                String text,
                long now);
    }

    interface NamePort {
        String resolve(String timelineId, String ownerId);
    }
}
