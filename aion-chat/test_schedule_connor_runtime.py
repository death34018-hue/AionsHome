import unittest

import schedule


class ScheduleConnorRuntimeTests(unittest.TestCase):
    def test_background_wakeups_resolve_connor_stream_from_route_module(self):
        loader = getattr(schedule, "_load_connor_stream_runtime", None)
        self.assertTrue(callable(loader), "Connor background runtime loader is missing")

        resolve_model, stream_model, load_config = loader()

        self.assertEqual(resolve_model.__module__, "routes.chatroom")
        self.assertEqual(stream_model.__module__, "routes.chatroom")
        self.assertEqual(load_config.__module__, "chatroom")


if __name__ == "__main__":
    unittest.main()
