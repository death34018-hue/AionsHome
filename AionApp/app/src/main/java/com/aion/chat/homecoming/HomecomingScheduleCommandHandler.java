package com.aion.chat.homecoming;

import java.nio.charset.StandardCharsets;
import java.text.ParseException;
import java.text.SimpleDateFormat;
import java.util.Calendar;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.TimeZone;
import java.util.UUID;

public final class HomecomingScheduleCommandHandler {
    private static final long STALE_GRACE_MS = 90_000L;
    private static final int MAX_CONTENT_LENGTH = 2_000;
    private static final int MAX_ID_LENGTH = 128;

    private final SchedulePort schedules;
    private final RegistrationPort registration;
    private final TimeZone timeZone;

    HomecomingScheduleCommandHandler(
            SchedulePort schedules, RegistrationPort registration, TimeZone timeZone) {
        if (schedules == null) throw new IllegalArgumentException("schedules are required");
        if (registration == null) {
            throw new IllegalArgumentException("registration is required");
        }
        if (timeZone == null) throw new IllegalArgumentException("timeZone is required");
        this.schedules = schedules;
        this.registration = registration;
        this.timeZone = timeZone;
    }

    public HomecomingScheduleCommandHandler(
            HomecomingScheduleRepository repository,
            RegistrationPort registration) {
        this(new RepositoryAdapter(repository), registration, TimeZone.getDefault());
    }

    public ApplyResult apply(
            String requestId,
            int index,
            HomecomingControlParser.ControlEvent event,
            String ownerId,
            String timelineId,
            long now) {
        if (event == null || requestId == null || requestId.trim().isEmpty()
                || index < 0) {
            return ApplyResult.invalid();
        }
        if ("schedule_list".equals(event.type)) {
            return new ApplyResult("listed", "", false);
        }
        if ("schedule_delete".equals(event.type)) {
            return delete(event.arguments, now);
        }
        if (!"alarm".equals(event.type) && !"reminder".equals(event.type)
                && !"monitor".equals(event.type)) {
            return new ApplyResult("deferred", "", false);
        }
        return create(requestId, index, event, ownerId, timelineId, now);
    }

    private ApplyResult create(
            String requestId,
            int index,
            HomecomingControlParser.ControlEvent event,
            String ownerId,
            String timelineId,
            long now) {
        List<String> values = event.arguments;
        if (values.size() != 2 || values.get(1).length() > MAX_CONTENT_LENGTH) {
            return ApplyResult.invalid();
        }
        Long triggerAt = parseTime(values.get(0), now);
        if (triggerAt == null || triggerAt < now - STALE_GRACE_MS) {
            return ApplyResult.invalid();
        }
        String id = UUID.nameUUIDFromBytes(
                (requestId.trim() + ":" + index + ":" + event.type)
                        .getBytes(StandardCharsets.UTF_8)).toString();
        HomecomingScheduleRepository.Schedule existing = schedules.find(id);
        if (existing != null) {
            return new ApplyResult("already_applied", id, false);
        }
        final HomecomingScheduleRepository.Schedule created;
        try {
            created = schedules.createWithId(
                    id, event.type, triggerAt, values.get(1),
                    ownerId, timelineId, now);
        } catch (RuntimeException exception) {
            return ApplyResult.invalid();
        }
        try {
            registration.register(created);
            return new ApplyResult("created", created.id, true);
        } catch (RuntimeException exception) {
            return new ApplyResult("registration_failed", created.id, true);
        }
    }

    private ApplyResult delete(List<String> values, long now) {
        if (values.size() != 1) return ApplyResult.invalid();
        String id = values.get(0).trim();
        if (id.isEmpty() || id.length() > MAX_ID_LENGTH) return ApplyResult.invalid();
        HomecomingScheduleRepository.Schedule current = schedules.find(id);
        if (current == null) return ApplyResult.invalid();
        if (!"active".equals(current.status)) {
            return new ApplyResult("already_applied", id, false);
        }
        try {
            schedules.delete(id, now);
            registration.cancel(id);
            return new ApplyResult("deleted", id, true);
        } catch (RuntimeException exception) {
            return new ApplyResult("delete_failed", id, false);
        }
    }

    private Long parseTime(String raw, long now) {
        String value = raw == null ? "" : raw.trim().replace('T', ' ');
        if (value.isEmpty()) return null;
        Calendar current = Calendar.getInstance(timeZone, Locale.ROOT);
        current.setTimeInMillis(now);
        if (value.matches("\\d{2}[-/]\\d{2}(?:\\s.*)?")) {
            value = current.get(Calendar.YEAR) + "-" + value.replace('/', '-');
        }
        if (value.matches("\\d{4}[-/]\\d{2}[-/]\\d{2}")) {
            value = value.replace('/', '-') + " 09:00";
        }
        String[] formats = {
                "yyyy-MM-dd HH:mm", "yyyy-MM-dd HH:mm:ss",
                "yyyy/MM/dd HH:mm", "yyyy/MM/dd HH:mm:ss"
        };
        for (String format : formats) {
            SimpleDateFormat parser = new SimpleDateFormat(format, Locale.ROOT);
            parser.setLenient(false);
            parser.setTimeZone(timeZone);
            try {
                Date parsed = parser.parse(value);
                if (parsed != null) return parsed.getTime();
            } catch (ParseException ignored) {
            }
        }
        return null;
    }

    interface SchedulePort {
        HomecomingScheduleRepository.Schedule find(String id);
        HomecomingScheduleRepository.Schedule createWithId(
                String id, String type, long triggerAt, String content,
                String ownerId, String timelineId, long now);
        HomecomingScheduleRepository.Schedule delete(String id, long now);
    }

    public interface RegistrationPort {
        void register(HomecomingScheduleRepository.Schedule schedule);
        void cancel(String scheduleId);
    }

    private static final class RepositoryAdapter implements SchedulePort {
        private final HomecomingScheduleRepository repository;
        RepositoryAdapter(HomecomingScheduleRepository repository) {
            if (repository == null) {
                throw new IllegalArgumentException("repository is required");
            }
            this.repository = repository;
        }
        @Override public HomecomingScheduleRepository.Schedule find(String id) {
            return repository.find(id);
        }
        @Override public HomecomingScheduleRepository.Schedule createWithId(
                String id, String type, long triggerAt, String content,
                String ownerId, String timelineId, long now) {
            return repository.createWithId(
                    id, type, triggerAt, content, ownerId, timelineId, now);
        }
        @Override public HomecomingScheduleRepository.Schedule delete(
                String id, long now) {
            return repository.delete(id, now);
        }
    }

    public static final class ApplyResult {
        public final String status;
        public final String scheduleId;
        public final boolean applied;

        ApplyResult(String status, String scheduleId, boolean applied) {
            this.status = status;
            this.scheduleId = scheduleId;
            this.applied = applied;
        }

        static ApplyResult invalid() {
            return new ApplyResult("invalid", "", false);
        }
    }
}
