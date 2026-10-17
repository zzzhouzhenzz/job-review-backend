"""Failed startup must not migrate a database or leak its reserved port."""
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from server import ReviewServer


class ServerStartupTests(unittest.TestCase):
    def test_occupied_port_does_not_open_or_create_database(self):
        with tempfile.TemporaryDirectory() as temp, socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            path = Path(temp) / "must-not-exist.sqlite3"
            with patch("server.WorkflowStore") as store:
                with self.assertRaises(OSError):
                    ReviewServer(occupied.getsockname(), path)
                store.assert_not_called()
            self.assertFalse(path.exists())

    def test_database_initialization_failure_releases_bound_port(self):
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            address = available.getsockname()
        with patch("server.WorkflowStore", side_effect=ValueError("fixture migration failed")):
            with self.assertRaisesRegex(ValueError, "fixture migration failed"):
                ReviewServer(address)
        with socket.socket() as retry:
            retry.bind(address)
            retry.listen()


if __name__ == "__main__":
    unittest.main()
