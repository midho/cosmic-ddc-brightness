import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from cosmic_ddc_brightness.config import MonitorConfig, render_config


PROJECT = Path(__file__).resolve().parents[1]
DETECT_SCRIPT = """\
if [ "$*" = "detect --brief" ]; then
    printf '%s\\n' \\
        'Display 1' \\
        '   I2C bus:             /dev/i2c-9' \\
        '   Monitor:             DEL:U2720Q:TEST123'
    exit 0
fi
"""
FAST_DDCUTIL = "#!/bin/sh\n" + DETECT_SCRIPT + """\
if printf '%s' "$*" | grep -q 'getvcp 10'; then
    printf '%s\\n' 'VCP 10 C 50 100'
    exit 0
fi
exit 2
"""
# Record a stuck read's pid so the test can stop the daemon mid-command.
STUCK_READ_DDCUTIL = "#!/bin/sh\n" + DETECT_SCRIPT + """\
if printf '%s' "$*" | grep -q 'getvcp 10'; then
    echo $$ > '{pid_file}'
    exec sleep 30
fi
exit 2
"""


def child_state(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return stat.rpartition(")")[2].split()[0]


def kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.fake_bin = self.root / "bin"
        self.runtime.mkdir(mode=0o700)
        self.fake_bin.mkdir()
        self.socket_path = self.runtime / "cosmic-ddc-brightness/daemon.sock"
        self.config = self.root / "config.toml"

    def write_config(self, **replacements: str) -> None:
        content = render_config([
            MonitorConfig("desk", "Desk", "DEL", "U2720Q", "TEST123")
        ]).replace("enabled = true", "enabled = false", 1)
        for old, new in replacements.items():
            self.assertIn(old, content)
            content = content.replace(old, new, 1)
        self.config.write_text(content, encoding="utf-8")

    def write_ddcutil(self, script: str) -> None:
        ddcutil = self.fake_bin / "ddcutil"
        ddcutil.write_text(script, encoding="utf-8")
        ddcutil.chmod(0o755)

    def stop_daemon(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
        process.communicate()

    def start_daemon(self) -> subprocess.Popen:
        environment = {
            **os.environ,
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
            "PYTHONPATH": str(PROJECT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_RUNTIME_DIR": str(self.runtime),
        }
        process = subprocess.Popen(
            [
                sys.executable, "-m", "cosmic_ddc_brightness",
                "--config", str(self.config), "daemon",
            ],
            cwd=PROJECT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop_daemon, process)
        return process

    def wait_ready(self, process: subprocess.Popen) -> str:
        ready_streams, _, _ = select.select([process.stdout], [], [], 3.0)
        if not ready_streams:
            process.kill()
            _, error = process.communicate()
            self.fail(f"daemon did not become ready: {error}")
        ready_line = process.stdout.readline()
        self.assertIn("controller is ready", ready_line)
        self.assertTrue(self.socket_path.exists())
        return ready_line

    def wait_for_child(self, pid_file: Path) -> int:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                text = pid_file.read_text().strip()
            except FileNotFoundError:
                text = ""
            if text and child_state(int(text)) is not None:
                return int(text)
            time.sleep(0.01)
        self.fail("the fake ddcutil read did not start")

    def test_sigterm_removes_socket_and_exits_cleanly(self):
        self.write_config()
        self.write_ddcutil(FAST_DDCUTIL)
        process = self.start_daemon()
        ready_line = self.wait_ready(process)

        process.send_signal(signal.SIGTERM)
        output, error = process.communicate(timeout=3.0)
        self.assertEqual(process.returncode, 0, error)
        self.assertIn("controller is ready", ready_line + output)
        self.assertFalse(self.socket_path.exists())

    def test_sigterm_during_ddc_command_stops_child_and_exits_cleanly(self):
        pid_file = self.root / "read.pid"
        self.write_config(**{"ddc_timeout_seconds = 3.0": "ddc_timeout_seconds = 10.0"})
        self.write_ddcutil(STUCK_READ_DDCUTIL.format(pid_file=pid_file))
        process = self.start_daemon()
        self.wait_ready(process)
        # The first read starts when the worker starts.
        child = self.wait_for_child(pid_file)
        self.addCleanup(kill_quietly, child)
        self.assertIsNotNone(child_state(child))

        process.send_signal(signal.SIGTERM)
        output, error = process.communicate(timeout=5.0)
        self.assertEqual(process.returncode, 0, error)
        self.assertNotIn("Traceback", error)
        self.assertNotIn("Traceback", output)
        self.assertFalse(self.socket_path.exists())
        # Check that shutdown leaves no running ddcutil orphan. test_ddc
        # checks reaping while the daemon is alive.
        self.assertIsNone(child_state(child))


if __name__ == "__main__":
    unittest.main()
