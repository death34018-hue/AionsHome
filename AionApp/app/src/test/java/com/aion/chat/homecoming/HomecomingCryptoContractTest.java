package com.aion.chat.homecoming;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Before;
import org.junit.Test;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.Signature;
import java.security.spec.MGF1ParameterSpec;
import java.security.spec.PSSParameterSpec;
import java.util.Arrays;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

public class HomecomingCryptoContractTest {
    private KeyPair keyPair;
    private HomecomingKeyStore.RouteCryptoCodec codec;

    @Before
    public void setUp() throws Exception {
        KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA");
        generator.initialize(2048);
        keyPair = generator.generateKeyPair();
        codec = new HomecomingKeyStore.RouteCryptoCodec(
                keyPair.getPrivate(), new MemorySecretStore());
    }

    @Test
    public void aadMustMatchDeviceAndSnapshot() throws Exception {
        JSONObject envelope = fixtureEnvelope(keyPair, "android:test-device", "snap-one");
        assertEquals("RSA-OAEP-256-MGF1-SHA1+A256GCM",
                envelope.getString("algorithm"));
        assertThrows(GeneralSecurityException.class,
                () -> codec.decryptAndStore(
                        envelope, "android:test-device", "snap-two"));
    }

    @Test
    public void wrongPrivateKeyCannotDecryptRoutes() throws Exception {
        KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA");
        generator.initialize(2048);
        HomecomingKeyStore.RouteCryptoCodec other = new HomecomingKeyStore.RouteCryptoCodec(
                generator.generateKeyPair().getPrivate(), new MemorySecretStore());
        JSONObject envelope = fixtureEnvelope(keyPair, "android:test-device", "snap-one");

        assertThrows(GeneralSecurityException.class,
                () -> other.decryptAndStore(
                        envelope, "android:test-device", "snap-one"));
    }

    @Test
    public void decryptedKeysNeverAppearInWebViewProjection() throws Exception {
        MemorySecretStore secrets = new MemorySecretStore();
        HomecomingKeyStore.RouteCryptoCodec storingCodec =
                new HomecomingKeyStore.RouteCryptoCodec(keyPair.getPrivate(), secrets);

        List<HomecomingKeyStore.RouteDescriptor> descriptors =
                storingCodec.decryptAndStore(
                        fixtureEnvelope(keyPair, "android:test-device", "snap-one"),
                        "android:test-device",
                        "snap-one");

        assertEquals(1, descriptors.size());
        assertEquals("Fixture Cloud", descriptors.get(0).label);
        assertFalse(descriptors.get(0).toJson().toString().contains("fixture-secret-key"));
        assertFalse(descriptors.get(0).toJson().has("api_key"));
        assertEquals("fixture-secret-key",
                new JSONObject(new String(secrets.value, StandardCharsets.UTF_8))
                        .getJSONArray("chat").getJSONObject(0).getString("api_key"));
    }

    @Test
    public void returnSigningCodecUsesRsaPssSha256() throws Exception {
        byte[] payload = "return package".getBytes(StandardCharsets.UTF_8);
        byte[] signature = new HomecomingKeyStore.ReturnSigningCodec(
                keyPair.getPrivate()).sign(payload);
        Signature verifier = Signature.getInstance("RSASSA-PSS");
        verifier.setParameter(new PSSParameterSpec(
                "SHA-256", "MGF1", MGF1ParameterSpec.SHA256, 32, 1));
        verifier.initVerify(keyPair.getPublic());
        verifier.update(payload);

        assertEquals("SHA256withRSA/PSS",
                HomecomingKeyStore.ReturnSigningCodec.ALGORITHM_LABEL);
        assertEquals(
                Arrays.asList("SHA256withRSA/PSS", "RSASSA-PSS"),
                HomecomingKeyStore.ReturnSigningCodec.algorithmCandidates());
        assertTrue(verifier.verify(signature));
    }

    static JSONObject fixtureEnvelope(
            KeyPair pair, String deviceId, String snapshotId) throws Exception {
        JSONObject route = new JSONObject()
                .put("route_id", "fixture-cloud")
                .put("label", "Fixture Cloud")
                .put("provider", "custom_openai")
                .put("base_url", "https://fixture.example/v1")
                .put("api_key", "fixture-secret-key")
                .put("models", new JSONArray().put(new JSONObject()
                        .put("key", "fixture-model")
                        .put("model", "fixture/model")
                        .put("vision", true)
                        .put("audio", false)));
        JSONObject routes = new JSONObject()
                .put("chat", new JSONArray().put(route))
                .put("services", new JSONObject());
        return HomecomingKeyStore.RouteCryptoCodec.encryptForTest(
                routes, pair.getPublic(), deviceId, snapshotId);
    }

    private static final class MemorySecretStore
            implements HomecomingKeyStore.SecretStore {
        byte[] value;

        @Override
        public void store(byte[] plaintext) {
            value = plaintext.clone();
        }
    }
}
