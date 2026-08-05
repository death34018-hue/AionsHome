package com.aion.chat.homecoming;

import java.util.List;

public final class HomecomingChatEngine {
    private final RepositoryPort repository;
    private final ContextPort context;
    private final GatewayPort gateway;
    private final Clock clock;
    private final ControlPort controls;

    HomecomingChatEngine(
            RepositoryPort repository,
            ContextPort context,
            GatewayPort gateway,
            Clock clock) {
        this(repository, context, gateway, clock,
                (command, events, now) ->
                        repository.recordControls(command.requestId, events));
    }

    HomecomingChatEngine(
            RepositoryPort repository,
            ContextPort context,
            GatewayPort gateway,
            Clock clock,
            ControlPort controls) {
        this.repository = repository;
        this.context = context;
        this.gateway = gateway;
        this.clock = clock;
        this.controls = controls;
    }

    public HomecomingChatEngine(
            HomecomingChatRepository repository,
            HomecomingContextBuilder context,
            HomecomingModelGateway gateway) {
        this(repository, context, gateway,
                (HomecomingScheduleCommandHandler) null);
    }

    public HomecomingChatEngine(
            HomecomingChatRepository repository,
            HomecomingContextBuilder context,
            HomecomingModelGateway gateway,
            HomecomingScheduleCommandHandler scheduleCommands) {
        this(repository, context, gateway, scheduleCommands, null);
    }

    public HomecomingChatEngine(
            HomecomingChatRepository repository,
            HomecomingContextBuilder context,
            HomecomingModelGateway gateway,
            HomecomingControlDispatcher dispatcher) {
        this(repository, context, gateway, null, dispatcher);
    }

    private HomecomingChatEngine(
            HomecomingChatRepository repository,
            HomecomingContextBuilder context,
            HomecomingModelGateway gateway,
            HomecomingScheduleCommandHandler scheduleCommands,
            HomecomingControlDispatcher dispatcher) {
        this(
                new RepositoryPort() {
                    @Override public void commitUser(ChatCommand command, long now) {
                        repository.commitUserMessage(
                                command.requestId, command.timelineId, command.userSenderId,
                                command.text,
                                command.imageDataUrl.isEmpty() ? "" : "image",
                                now);
                    }
                    @Override public void prepareAssistant(
                            ChatCommand command, long now) {
                        repository.beginAssistantRequest(
                                command.requestId, command.timelineId, now);
                    }
                    @Override public String commitAssistant(
                            ChatCommand command, String text, long now) {
                        return repository.commitAssistantMessage(
                                command.requestId, command.timelineId,
                                command.responderOwner, text, now).id;
                    }
                    @Override public void fail(String requestId, String code) {
                        repository.failRequest(requestId, code);
                    }
                    @Override public void recordControls(
                            String requestId,
                            List<HomecomingControlParser.ControlEvent> controls) {
                        repository.recordDeferredControls(
                                requestId, controls, System.currentTimeMillis());
                    }
                },
                context::build,
                new GatewayPort() {
                    @Override public void stream(
                            HomecomingModelGateway.ChatRequest request,
                            HomecomingModelGateway.StreamObserver observer) throws Exception {
                        gateway.stream(request, observer);
                    }
                    @Override public void cancel(String requestId) {
                        gateway.cancel(requestId);
                    }
                },
                System::currentTimeMillis,
                dispatcher != null
                        ? dispatcher
                        : scheduleCommands == null
                        ? (command, controls, now) ->
                                repository.recordDeferredControls(
                                        command.requestId, controls, now)
                        : (command, controls, now) -> {
                            for (int index = 0; index < controls.size(); index++) {
                                HomecomingControlParser.ControlEvent control =
                                        controls.get(index);
                                HomecomingScheduleCommandHandler.ApplyResult result =
                                        scheduleCommands.apply(
                                                command.requestId,
                                                index,
                                                control,
                                                command.responderOwner,
                                                command.timelineId,
                                                now);
                                if ("deferred".equals(result.status)) {
                                    repository.recordDeferredControls(
                                            command.requestId,
                                            java.util.Collections.singletonList(control),
                                            now);
                                }
                            }
                        });
    }

    public void send(ChatCommand command, Observer observer) {
        final List<HomecomingModelGateway.ChatMessage> messages;
        final long now = clock.now();
        try {
            messages = context.build(
                    command.timelineId,
                    command.responderOwner,
                    command.text,
                    now,
                    command.locationState);
            if (command.commitUser) {
                repository.commitUser(command, now);
            } else {
                repository.prepareAssistant(command, now);
            }
            gateway.stream(new HomecomingModelGateway.ChatRequest(
                    command.requestId,
                    command.routeId,
                    command.modelKey,
                    messages,
                    command.imageDataUrl),
                    new HomecomingModelGateway.StreamObserver() {
                        @Override public void onChunk(String text) {
                            // Control tags may span chunks; publish only after final parsing.
                        }

                        @Override public void onComplete(String text) {
                            HomecomingControlParser.Result parsed =
                                    HomecomingControlParser.parse(text);
                            try {
                                String messageId = repository.commitAssistant(
                                        command, parsed.visibleText, clock.now());
                                controls.apply(command, parsed.events, clock.now());
                                if (!parsed.visibleText.isEmpty()) {
                                    observer.onChunk(parsed.visibleText);
                                }
                                observer.onComplete(
                                        messageId,
                                        parsed.visibleText);
                            } catch (RuntimeException exception) {
                                repository.fail(command.requestId, "assistant_commit_failed");
                                observer.onFailure("assistant_commit_failed");
                            }
                        }

                        @Override public void onFailure(String code) {
                            repository.fail(command.requestId, code);
                            observer.onFailure(code);
                        }
                    });
        } catch (Exception exception) {
            try {
                repository.fail(command.requestId, "request_setup_failed");
            } catch (RuntimeException ignored) {
                // The user commit may not exist yet.
            }
            observer.onFailure("request_setup_failed");
        }
    }

    public void stop(String requestId) {
        gateway.cancel(requestId);
    }

    interface RepositoryPort {
        void commitUser(ChatCommand command, long now);
        default void prepareAssistant(ChatCommand command, long now) {
        }
        String commitAssistant(ChatCommand command, String text, long now);
        void fail(String requestId, String code);
        void recordControls(
                String requestId, List<HomecomingControlParser.ControlEvent> controls);
    }

    interface ContextPort {
        List<HomecomingModelGateway.ChatMessage> build(
                String timelineId, String responderOwner, String trigger,
                long now, String locationState);
    }

    interface GatewayPort {
        void stream(
                HomecomingModelGateway.ChatRequest request,
                HomecomingModelGateway.StreamObserver observer) throws Exception;
        void cancel(String requestId);
    }

    interface Clock {
        long now();
    }

    interface ControlPort {
        void apply(
                ChatCommand command,
                List<HomecomingControlParser.ControlEvent> controls,
                long now);
    }

    public interface Observer {
        void onChunk(String text);
        void onComplete(String messageId, String text);
        void onFailure(String code);
    }

    public static final class ChatCommand {
        public final String requestId;
        public final String timelineId;
        public final String responderOwner;
        public final String userSenderId;
        public final String text;
        public final String routeId;
        public final String modelKey;
        public final String imageDataUrl;
        public final String locationState;
        public final boolean commitUser;

        public ChatCommand(
                String requestId, String timelineId, String responderOwner,
                String userSenderId, String text, String routeId, String modelKey,
                String imageDataUrl, String locationState) {
            this(requestId, timelineId, responderOwner, userSenderId, text,
                    routeId, modelKey, imageDataUrl, locationState, true);
        }

        public ChatCommand(
                String requestId, String timelineId, String responderOwner,
                String userSenderId, String text, String routeId, String modelKey,
                String imageDataUrl, String locationState, boolean commitUser) {
            this.requestId = required(requestId, "requestId");
            this.timelineId = required(timelineId, "timelineId");
            this.responderOwner = required(responderOwner, "responderOwner");
            this.userSenderId = required(userSenderId, "userSenderId");
            this.text = text == null ? "" : text;
            this.routeId = required(routeId, "routeId");
            this.modelKey = required(modelKey, "modelKey");
            this.imageDataUrl = imageDataUrl == null ? "" : imageDataUrl;
            this.locationState = locationState == null ? "" : locationState;
            this.commitUser = commitUser;
        }
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }
}
