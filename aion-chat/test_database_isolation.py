import unittest
from pathlib import Path

import config


class DatabaseIsolationTests(unittest.TestCase):
    def test_test_process_never_points_at_live_database(self):
        live_database = (config.DATA_DIR / "chat.db").resolve()

        self.assertNotEqual(live_database, config.DB_PATH.resolve())
        self.assertIn("aionshome-tests-", str(config.DB_PATH.parent).lower())


if __name__ == "__main__":
    unittest.main()
