package com.aion.chat.homecoming;

import org.json.JSONArray;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class HomecomingRouteVault {
    private final Map<String, Route> routes;
    private final Map<String, Service> services;

    private HomecomingRouteVault(Map<String, Route> routes, Map<String, Service> services) {
        this.routes = Collections.unmodifiableMap(new LinkedHashMap<>(routes));
        this.services = Collections.unmodifiableMap(new LinkedHashMap<>(services));
    }

    public static HomecomingRouteVault fromPlaintext(byte[] plaintext) throws Exception {
        JSONObject root = new JSONObject(new String(plaintext, StandardCharsets.UTF_8));
        JSONArray values = root.optJSONArray("chat");
        LinkedHashMap<String, Route> routes = new LinkedHashMap<>();
        if (values != null) {
            for (int i = 0; i < values.length(); i++) {
                JSONObject value = values.getJSONObject(i);
                ArrayList<Model> models = new ArrayList<>();
                JSONArray modelValues = value.optJSONArray("models");
                if (modelValues != null) {
                    for (int j = 0; j < modelValues.length(); j++) {
                        JSONObject model = modelValues.getJSONObject(j);
                        models.add(new Model(
                                required(model.optString("key", ""), "model key"),
                                required(model.optString("model", ""), "model"),
                                model.optBoolean("vision", false),
                                model.optBoolean("audio", false)));
                    }
                }
                Route route = new Route(
                        required(value.optString("route_id", ""), "route id"),
                        value.optString("label", value.optString("route_id", "")),
                        required(value.optString("provider", ""), "provider"),
                        required(value.optString("base_url", ""), "base url"),
                        required(value.optString("api_key", ""), "api key"),
                        models);
                if (routes.put(route.routeId, route) != null) {
                    throw new IllegalArgumentException("duplicate route id");
                }
            }
        }
        LinkedHashMap<String, Service> services = new LinkedHashMap<>();
        JSONObject serviceValues = root.optJSONObject("services");
        if (serviceValues != null) {
            java.util.Iterator<String> names = serviceValues.keys();
            while (names.hasNext()) {
                String name = names.next();
                JSONObject value = serviceValues.getJSONObject(name);
                services.put(name, new Service(
                        name,
                        required(value.optString("provider", ""), "service provider"),
                        required(value.optString("base_url", ""), "service base url"),
                        required(value.optString("api_key", ""), "service api key"),
                        value.optString("model", ""),
                        value.optBoolean("enabled", true),
                        value.optString("main_voice", ""),
                        value.optString("second_voice", "")));
            }
        }
        return new HomecomingRouteVault(routes, services);
    }

    public List<HomecomingKeyStore.RouteDescriptor> listDescriptors() {
        ArrayList<HomecomingKeyStore.RouteDescriptor> descriptors = new ArrayList<>();
        for (Route route : routes.values()) {
            ArrayList<String> keys = new ArrayList<>();
            boolean vision = false;
            boolean audio = false;
            for (Model model : route.models) {
                keys.add(model.key);
                vision |= model.vision;
                audio |= model.audio;
            }
            descriptors.add(new HomecomingKeyStore.RouteDescriptor(
                    route.routeId, route.label, route.provider,
                    keys, vision, audio, true));
        }
        return Collections.unmodifiableList(descriptors);
    }

    public Route resolve(String routeId) {
        Route route = routes.get(routeId);
        if (route == null) {
            throw new IllegalArgumentException("unknown Homecoming route");
        }
        return route;
    }

    public Service resolveService(String serviceId) {
        Service service = services.get(serviceId);
        if (service == null) {
            throw new IllegalArgumentException("unknown Homecoming service");
        }
        return service;
    }

    public static final class Route {
        public final String routeId;
        public final String label;
        public final String provider;
        public final String baseUrl;
        public final String apiKey;
        public final List<Model> models;

        Route(String routeId, String label, String provider, String baseUrl,
                String apiKey, List<Model> models) {
            this.routeId = routeId;
            this.label = label;
            this.provider = provider;
            this.baseUrl = baseUrl;
            this.apiKey = apiKey;
            this.models = Collections.unmodifiableList(new ArrayList<>(models));
        }

        public Model model(String key) {
            for (Model model : models) {
                if (model.key.equals(key)) {
                    return model;
                }
            }
            throw new IllegalArgumentException("unknown Homecoming model");
        }
    }

    public static final class Model {
        public final String key;
        public final String model;
        public final boolean vision;
        public final boolean audio;

        Model(String key, String model, boolean vision, boolean audio) {
            this.key = key;
            this.model = model;
            this.vision = vision;
            this.audio = audio;
        }
    }

    public static final class Service {
        public final String serviceId;
        public final String provider;
        public final String baseUrl;
        public final String apiKey;
        public final String model;
        public final boolean enabled;
        public final String mainVoice;
        public final String secondVoice;

        Service(String serviceId, String provider, String baseUrl, String apiKey,
                String model, boolean enabled, String mainVoice, String secondVoice) {
            this.serviceId = serviceId;
            this.provider = provider;
            this.baseUrl = baseUrl;
            this.apiKey = apiKey;
            this.model = model;
            this.enabled = enabled;
            this.mainVoice = mainVoice;
            this.secondVoice = secondVoice;
        }
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }
}
