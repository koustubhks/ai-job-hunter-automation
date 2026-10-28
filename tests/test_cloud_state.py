import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

import cloud_state


class CloudStateTests(unittest.TestCase):
    def test_seed_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state" / "job_hunter_state.sqlite"
            database.parent.mkdir()
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE jobs (uid TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO jobs VALUES ('existing-job')")
            connection.commit()
            connection.close()
            key = Fernet.generate_key().decode()
            with patch.object(cloud_state, "DB", database), \
                 patch.object(cloud_state, "OUTPUT", root / "state.enc"), \
                 patch.object(cloud_state, "SEED", root / "cloud_state_seed.enc"), \
                 patch.dict(os.environ, {"STATE_KEY": key}), \
                 patch.object(cloud_state, "latest_artifact", return_value=None), \
                 patch.object(cloud_state, "latest_previous_run", return_value=None):
                cloud_state.save()
                cloud_state.SEED.write_bytes(cloud_state.OUTPUT.read_bytes())
                database.unlink()
                cloud_state.restore()
                connection = sqlite3.connect(database)
                self.assertEqual(connection.execute("SELECT uid FROM jobs").fetchone()[0], "existing-job")
                connection.close()

    def test_missing_artifact_after_prior_run_fails_closed(self):
        with patch.object(cloud_state, "latest_artifact", return_value=None), \
             patch.object(cloud_state, "latest_previous_run", return_value={"id": 123}):
            with self.assertRaisesRegex(RuntimeError, "No state artifact"):
                cloud_state.restore()


if __name__ == "__main__":
    unittest.main()
