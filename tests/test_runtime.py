import os
import socket
import tempfile
import unittest
from pathlib import Path

from cosmic_ddc_brightness.runtime import (
    acquire_instance_lock,
    decode_command,
    encode_command,
    prepare_runtime_directory,
    session_from_environment,
)


class RuntimeTests(unittest.TestCase):
    def test_command_round_trip(self):
        action, display = decode_command(encode_command("up", Path("/missing")))
        self.assertEqual(action, "up")
        self.assertEqual(display, "")

    def test_geekmagic_toggle_command_round_trip(self):
        action, display = decode_command(
            encode_command("toggle-geekmagic", Path("/missing"))
        )
        self.assertEqual(action, "toggle-geekmagic")
        self.assertEqual(display, "")

    def test_invalid_action_is_rejected(self):
        with self.assertRaises(ValueError):
            decode_command(b'{"action":"erase"}')

    def test_second_daemon_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = prepare_runtime_directory(Path(temporary) / "runtime")
            first = acquire_instance_lock(directory)
            try:
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    acquire_instance_lock(directory)
            finally:
                first.close()

    def test_wayland_socket_is_passed_to_daemon(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            display_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            display_socket.bind(str(base / "wayland-9"))
            old_display = os.environ.get("WAYLAND_DISPLAY")
            os.environ["WAYLAND_DISPLAY"] = "wayland-9"
            try:
                self.assertEqual(
                    session_from_environment(base), {"wayland_display": "wayland-9"}
                )
                self.assertEqual(decode_command(encode_command("show", base))[1], "wayland-9")
            finally:
                display_socket.close()
                if old_display is None:
                    os.environ.pop("WAYLAND_DISPLAY", None)
                else:
                    os.environ["WAYLAND_DISPLAY"] = old_display


if __name__ == "__main__":
    unittest.main()
