import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from network_check import router


class NetworkCheckTests(unittest.TestCase):
    def test_probe_counts_bytes_and_rejects_oversized_uploads(self):
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        self.assertEqual(client.get('/api/network-check/ping').headers['cache-control'], 'no-store')
        self.assertEqual(client.post('/api/network-check/upload', content=b'x' * (256 * 1024)).json(), {'bytes': 256 * 1024})
        self.assertEqual(client.post('/api/network-check/upload', content=b'x' * (512 * 1024 + 1)).status_code, 413)
