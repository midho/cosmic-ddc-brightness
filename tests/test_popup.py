import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, patch

from cosmic_ddc_brightness.config import PopupConfig
from cosmic_ddc_brightness.popup import BrightnessPopup, display_value


def message(name: str) -> dict:
    return {
        "show": True,
        "title": "Brightness · All monitors",
        "levels": [
            {
                "name": name,
                "percent": 50,
                "estimated": False,
                "status": "",
                "selected": True,
            }
        ],
    }


class ClosingStream:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class ExitedBeforeKillProcess:
    """The kernel has exited the child, but its watcher has not caught up."""

    def __init__(self):
        self.stdin = ClosingStream()
        self.returncode = None
        self.kill_calls = 0

    async def wait(self):
        await asyncio.Event().wait()

    def kill(self):
        self.kill_calls += 1
        raise ProcessLookupError


class PopupValueTests(unittest.TestCase):
    def test_shows_estimated_percent_instead_of_updating_status(self):
        self.assertEqual(
            display_value({"percent": 49, "estimated": True, "status": "Updating…"}),
            ("≈49%", 0.49),
        )

    def test_keeps_unconfirmed_warning_with_percent(self):
        self.assertEqual(
            display_value({"percent": 49, "estimated": True, "status": "Not confirmed"}),
            ("≈49% (not confirmed)", 0.49),
        )

    def test_shows_status_when_no_percent_is_known(self):
        self.assertEqual(
            display_value({"percent": None, "status": "Reading…"}),
            ("Reading…", 0.0),
        )


class PopupBackpressureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Give publish() a fake Wayland session. Mock the process start.
        self.enterContext(patch.object(BrightnessPopup, "environment", return_value={}))

    async def start_child(self, program: str):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.addAsyncCleanup(self.stop_process, process)
        # Wait for the child to announce that it is ready.
        self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 5.0), b"ready\n")
        return process

    async def stop_process(self, process):
        if process.returncode is None:
            process.kill()
            await process.wait()

    async def assert_start_attempted(self, popup, expected: bool):
        with patch(
            "cosmic_ddc_brightness.popup.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=OSError("start suppressed by the test"),
        ) as start:
            await popup.publish(message("Desk"))
        self.assertEqual(start.await_count, 1 if expected else 0)

    async def test_oversized_update_is_dropped_before_starting_popup(self):
        popup = BrightnessPopup(PopupConfig())
        with patch(
            "cosmic_ddc_brightness.popup.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
        ) as start:
            await popup.publish(message("x" * 1_000_000))
        start.assert_not_awaited()
        self.assertIsNone(popup.process)

    async def test_nonreading_popup_cannot_block_publish(self):
        popup = BrightnessPopup(PopupConfig())
        process = await self.start_child(
            "import sys, time; print('ready', flush=True); time.sleep(30)"
        )
        popup.process = process

        # Keep writing until the pipe fills. Its capacity varies by machine.
        update = message("x" * 60_000)
        for _ in range(64):
            await asyncio.wait_for(popup.publish(update), 2.0)
            if popup.process is None:
                break
        self.assertIsNone(popup.process)
        self.assertIsNotNone(process.returncode)
        # A stuck child triggers the restart pause.
        await self.assert_start_attempted(popup, expected=False)

    async def test_disconnected_popup_is_replaced_on_the_next_action(self):
        popup = BrightnessPopup(PopupConfig())
        process = await self.start_child(
            "import os, time; os.close(0); print('ready', flush=True); time.sleep(30)"
        )
        popup.process = process

        await asyncio.wait_for(popup.publish(message("Desk")), 2.0)
        self.assertIsNone(popup.process)
        self.assertIsNotNone(process.returncode)
        # A closed child can restart immediately.
        await self.assert_start_attempted(popup, expected=True)

    async def test_close_is_bounded_when_process_exits_during_kill(self):
        popup = BrightnessPopup(PopupConfig())
        process = ExitedBeforeKillProcess()
        popup.process = process
        with (
            patch("cosmic_ddc_brightness.popup.POPUP_CLOSE_GRACE_SECONDS", 0.01),
            patch("cosmic_ddc_brightness.popup.POPUP_STOP_TIMEOUT_SECONDS", 0.01),
        ):
            await asyncio.wait_for(popup.close(), 0.2)
        self.assertIsNone(popup.process)
        self.assertTrue(process.stdin.closed)
        self.assertEqual(process.kill_calls, 1)


if __name__ == "__main__":
    unittest.main()
