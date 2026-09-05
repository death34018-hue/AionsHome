import unittest

import camera


class CameraConnorRuntimeTests(unittest.TestCase):
    def test_sentinel_wakeup_resolves_connor_model_from_route_module(self):
        loader = getattr(camera, "_load_connor_model_resolver", None)
        self.assertTrue(callable(loader), "Connor sentinel model resolver loader is missing")

        resolve_model = loader()

        self.assertEqual(resolve_model.__module__, "routes.chatroom")


if __name__ == "__main__":
    unittest.main()
