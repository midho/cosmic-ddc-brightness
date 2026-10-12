import asyncio
import json
import os
import shutil
import signal
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cosmic_ddc_brightness.config import MonitorConfig
from cosmic_ddc_brightness.ddc import (
    AmbiguousMonitorError,
    DdcClient,
    DdcCommandError,
    DdcManager,
    DetectedMonitor,
    configurations_from_detected,
    match_monitors,
    parse_brightness,
    parse_connector_output,
    parse_detect_output,
    edid_identity,
)


DETECT_OUTPUT = """\
Display 1
   I2C bus:             /dev/i2c-7
   Monitor:             ACR:Acer X243W:LAG040064310

Display 2
   I2C bus:             /dev/i2c-14
   Monitor:             DEL:DELL U3011:PH5NY2CIANXL
"""


class ParserTests(unittest.TestCase):
    def test_detect_brief_output(self):
        monitors = parse_detect_output(DETECT_OUTPUT)
        self.assertEqual(len(monitors), 2)
        self.assertEqual(monitors[1].bus, 14)
        self.assertEqual(monitors[1].serial, "PH5NY2CIANXL")

    def test_brightness_output(self):
        self.assertEqual(parse_brightness("VCP 10 C 127 254\n"), (127, 254))
        self.assertIsNone(parse_brightness("VCP 10 C 300 254\n"))

    def test_matching_ignores_bus_number(self):
        configured = (MonitorConfig("desk", "Desk", "DEL", "DELL U3011", "PH5NY2CIANXL"),)
        first = parse_detect_output(DETECT_OUTPUT)
        moved = [DetectedMonitor(2, 23, "DEL", "DELL U3011", "PH5NY2CIANXL")]
        self.assertEqual(match_monitors(configured, first)["desk"].bus, 14)
        self.assertEqual(match_monitors(configured, moved)["desk"].bus, 23)

    def test_missing_configured_serial_is_rejected_even_with_one_monitor(self):
        configured = (MonitorConfig("desk", "Desk", "DEL", "U2720Q", ""),)
        detected = [DetectedMonitor(1, 4, "DEL", "U2720Q", "ONE")]
        with self.assertRaisesRegex(DdcCommandError, "serial number"):
            match_monitors(configured, detected)

    def test_auto_configuration_rejects_duplicate_identity(self):
        monitor = DetectedMonitor(1, 4, "DEL", "U2720Q", "SAME")
        with self.assertRaises(AmbiguousMonitorError):
            configurations_from_detected([monitor, DetectedMonitor(2, 5, "DEL", "U2720Q", "SAME")])

    def test_auto_configuration_rejects_missing_serial(self):
        with self.assertRaisesRegex(DdcCommandError, "serial number"):
            configurations_from_detected([DetectedMonitor(1, 4, "DEL", "U2720Q", "")])


class IdentityCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_select_full_identity_instead_of_bus(self):
        client = DdcClient()
        client.command = AsyncMock(return_value="VCP 10 C 60 100")
        monitor = DetectedMonitor(1, 4, "DEL", "MODEL WITH SPACES", "SERIAL")
        self.assertEqual(await client.read_brightness(monitor), (60, 100))
        client.command.assert_awaited_once_with(
            "--mfg", "DEL", "--model", "MODEL WITH SPACES", "--sn", "SERIAL",
            "--skip-ddc-checks", "--disable-dynamic-sleep", "--sleep-multiplier", "1",
            "--brief", "getvcp", "10",
        )
        client.command.reset_mock()
        await client.write_brightness(monitor, 40)
        client.command.assert_awaited_once_with(
            "--mfg", "DEL", "--model", "MODEL WITH SPACES", "--sn", "SERIAL",
            "--skip-ddc-checks", "--disable-dynamic-sleep", "--sleep-multiplier", "1",
            "--noverify", "setvcp", "10", "40",
        )

    async def test_missing_detected_serial_does_not_start_command(self):
        client = DdcClient()
        client.command = AsyncMock()
        monitor = DetectedMonitor(1, 4, "DEL", "U2720Q", "")
        with self.assertRaisesRegex(DdcCommandError, "serial number"):
            await client.write_brightness(monitor, 40)
        with self.assertRaisesRegex(DdcCommandError, "serial number"):
            await client.read_brightness(monitor)
        client.command.assert_not_awaited()


def sample_edid(manufacturer="DEL", model="DELL U3011", serial="SERIAL"):
    data = bytearray(128)
    data[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    code = sum((ord(letter) - 64) << shift for letter, shift in zip(manufacturer, (10, 5, 0)))
    data[8:10] = code.to_bytes(2, "big")
    for start, tag, value in ((54, 0xFC, model), (72, 0xFF, serial)):
        data[start:start + 18] = b"\x00\x00\x00" + bytes((tag, 0)) + value.encode().ljust(13, b" ")
    data[127] = -sum(data[:127]) % 256
    return bytes(data)


class GuardedBusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.monitor = DetectedMonitor(1, 4, "DEL", "DELL U3011", "SERIAL")
        self.config = MonitorConfig("desk", "Desk", "DEL", "DELL U3011", "SERIAL")
        self.client = DdcClient(sysfs_root=self.root)
        self.client.detect = AsyncMock(return_value=[self.monitor])
        self.client.detect_connectors = AsyncMock(return_value={4: "card0-DP-1"})
        self.client.command = AsyncMock(return_value="")
        self.connector("card0-DP-1", sample_edid())
        (self.root / "bus/i2c/devices/i2c-4").mkdir(parents=True)

    async def asyncTearDown(self):
        self.directory.cleanup()

    def connector(self, name, edid):
        folder = self.root / "class/drm" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "status").write_text("connected\n")
        (folder / "edid").write_bytes(edid)

    def manager(self):
        return DdcManager((self.config,), self.client, scan_before_each_write=False)

    async def test_direct_bus_write_follows_matching_edid(self):
        manager = self.manager()
        await manager.refresh()
        await manager.write("desk", 40)
        self.client.command.assert_awaited_once_with(
            "--bus", "4", "--skip-ddc-checks", "--disable-dynamic-sleep",
            "--sleep-multiplier", "1", "--noverify", "setvcp", "10", "40",
            guard=manager.bus_guards["desk"],
        )

    async def test_replacement_on_old_bus_never_gets_write(self):
        manager = self.manager()
        await manager.refresh()
        self.connector("card0-DP-1", sample_edid(serial="REPLACED"))
        with self.assertRaisesRegex(DdcCommandError, "identity or bus changed"):
            await manager.write("desk", 40)
        self.client.command.assert_not_awaited()

    async def test_original_monitor_moves_to_new_bus(self):
        manager = self.manager()
        await manager.refresh()
        self.connector("card0-DP-1", sample_edid(serial="REPLACED"))
        self.connector("card0-DP-2", sample_edid())
        (self.root / "bus/i2c/devices/i2c-12").mkdir(parents=True)
        self.client.detect.return_value = [DetectedMonitor(1, 12, "DEL", "DELL U3011", "SERIAL")]
        self.client.detect_connectors.return_value = {12: "card0-DP-2"}
        await manager.write("desk", 40)
        self.assertEqual(self.client.command.await_args.args[:2], ("--bus", "12"))

    async def test_missing_sysfs_identity_uses_edid_selector(self):
        (self.root / "class/drm/card0-DP-1/edid").write_bytes(b"")
        manager = self.manager()
        await manager.refresh()
        await manager.write("desk", 40)
        self.assertEqual(self.client.command.await_args.args[:2], ("--mfg", "DEL"))

    async def test_disconnected_connector_blocks_old_bus(self):
        manager = self.manager()
        await manager.refresh()
        (self.root / "class/drm/card0-DP-1/status").write_text("disconnected\n")
        with self.assertRaisesRegex(DdcCommandError, "identity or bus changed"):
            await manager.write("desk", 40)
        self.client.command.assert_not_awaited()

    async def test_changed_bus_path_blocks_old_bus(self):
        bus = self.root / "bus/i2c/devices/i2c-4"
        bus.rmdir()
        first = self.root / "bus/i2c/adapters/first"
        second = self.root / "bus/i2c/adapters/second"
        first.mkdir(parents=True)
        second.mkdir()
        bus.symlink_to(first)
        manager = self.manager()
        await manager.refresh()
        bus.unlink()
        bus.symlink_to(second)
        with self.assertRaisesRegex(DdcCommandError, "identity or bus changed"):
            await manager.write("desk", 40)
        self.client.command.assert_not_awaited()

    async def test_missing_monitor_does_not_rescan_unchanged_outputs(self):
        missing = MonitorConfig("missing", "Unplugged", "DEL", "DELL U3011", "OTHER")
        manager = DdcManager((missing, self.config), self.client, scan_before_each_write=False)
        await manager.refresh()
        self.client.detect.reset_mock()
        self.client.detect_connectors.reset_mock()
        for _ in range(5):
            with self.assertRaisesRegex(DdcCommandError, "not connected"):
                await manager.read("missing")
            with self.assertRaisesRegex(DdcCommandError, "not connected"):
                await manager.write("missing", 40)
        self.client.detect.assert_not_awaited()
        self.client.detect_connectors.assert_not_awaited()
        self.client.command.assert_not_awaited()

    async def test_cached_missing_monitor_reconnects_after_output_change(self):
        missing = MonitorConfig("missing", "Unplugged", "DEL", "DELL U3011", "OTHER")
        manager = DdcManager((missing, self.config), self.client, scan_before_each_write=False)
        await manager.refresh()
        with self.assertRaisesRegex(DdcCommandError, "not connected"):
            await manager.read("missing")
        self.connector("card0-DP-2", sample_edid(serial="OTHER"))
        (self.root / "bus/i2c/devices/i2c-12").mkdir(parents=True)
        self.client.detect.return_value = [self.monitor, DetectedMonitor(2, 12, "DEL", "DELL U3011", "OTHER")]
        self.client.detect_connectors.return_value = {4: "card0-DP-1", 12: "card0-DP-2"}
        self.client.command.return_value = "VCP 10 C 60 100"
        self.assertEqual(await manager.read("missing"), (60, 100))
        self.assertEqual(manager.matches["missing"].bus, 12)

    async def test_physically_connected_monitor_is_not_cached_as_unplugged(self):
        self.client.detect.return_value = []
        manager = self.manager()
        await manager.refresh()
        self.client.detect.reset_mock()
        with self.assertRaisesRegex(DdcCommandError, "not connected"):
            await manager.read("desk")
        self.client.detect.assert_awaited_once()

    async def test_fast_read_uses_the_checked_bus(self):
        manager = self.manager()
        await manager.refresh()
        self.client.command.return_value = "VCP 10 C 60 100"
        self.assertEqual(await manager.read("desk"), (60, 100))
        self.assertEqual(self.client.command.await_args.args[:2], ("--bus", "4"))

    async def test_replaced_identity_blocks_a_fast_read(self):
        manager = self.manager()
        await manager.refresh()
        self.connector("card0-DP-1", sample_edid(serial="REPLACED"))
        with self.assertRaisesRegex(DdcCommandError, "identity or bus changed"):
            await manager.read("desk")
        self.client.command.assert_not_awaited()

    async def test_queued_bus_write_rechecks_identity_after_waiting(self):
        client = DdcClient(sysfs_root=self.root)
        client._command = AsyncMock(return_value="")
        guard = client.prepare_bus_guard(self.monitor, "card0-DP-1")
        self.assertIsNotNone(guard)
        await client._command_lock.acquire()
        write = asyncio.create_task(client.write_brightness_bus(guard, 40))
        try:
            await asyncio.sleep(0)
            self.connector("card0-DP-1", sample_edid(serial="REPLACED"))
            client._command_lock.release()
            with self.assertRaisesRegex(DdcCommandError, "identity or bus changed"):
                await write
            client._command.assert_not_awaited()
        finally:
            if client._command_lock.locked():
                client._command_lock.release()
            if not write.done():
                write.cancel()
            await asyncio.gather(write, return_exceptions=True)

    async def test_missing_monitor_is_not_cached_when_output_state_is_unreadable(self):
        missing = MonitorConfig("missing", "Unplugged", "DEL", "DELL U3011", "OTHER")
        (self.root / "class/drm/card0-DP-1/status").unlink()
        manager = DdcManager((missing, self.config), self.client, scan_before_each_write=False)
        await manager.refresh()
        self.client.detect.reset_mock()
        with self.assertRaisesRegex(DdcCommandError, "not connected"):
            await manager.read("missing")
        self.client.detect.assert_awaited_once()

    def test_parser_rejects_invalid_or_duplicate_mapping(self):
        output = "Display 1\n I2C bus: /dev/i2c-4\n DRM_connector: card0-DP-1\n"
        self.assertEqual(parse_connector_output(output), {4: "card0-DP-1"})
        self.assertEqual(parse_connector_output(output + output.replace("Display 1", "Display 2")), {})
        self.assertEqual(edid_identity(sample_edid()), ("DEL", "DELL U3011", "SERIAL"))
        self.assertIsNone(edid_identity(b"bad"))


class FakeDdcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name) / "state"
        self.state.write_text("127", encoding="utf-8")
        self.program = Path(self.directory.name) / "ddcutil"
        self.program.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import pathlib
            import sys
            state = pathlib.Path({str(self.state)!r})
            args = sys.argv[1:]
            if args == ["detect", "--brief"]:
                print({DETECT_OUTPUT!r})
            elif "getvcp" in args:
                print("VCP 10 C " + state.read_text() + " 254")
            elif "setvcp" in args:
                state.write_text(args[-1])
            elif args == ["--version"]:
                print("ddcutil 2.2.7")
            else:
                raise SystemExit(2)
        """), encoding="utf-8")
        self.program.chmod(0o755)
        self.client = DdcClient(str(self.program), timeout=1.0)

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_fake_detect_read_and_write(self):
        detected = await self.client.detect()
        self.assertEqual([item.bus for item in detected], [7, 14])
        self.assertEqual(await self.client.read_brightness(detected[1]), (127, 254))
        await self.client.write_brightness(detected[1], 140)
        self.assertEqual(await self.client.read_brightness(detected[1]), (140, 254))

    async def test_read_and_scan_do_not_compete_for_the_monitor(self):
        lock = Path(self.directory.name) / "monitor-lock"
        self.program.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import pathlib, sys, time
            lock = pathlib.Path({str(lock)!r})
            try:
                lock.touch(exist_ok=False)
            except FileExistsError:
                print("monitor is busy", file=sys.stderr)
                raise SystemExit(1)
            try:
                time.sleep(0.15)
                if sys.argv[1:] == ["detect", "--brief"]:
                    print({DETECT_OUTPUT!r})
                else:
                    print("VCP 10 C 50 100")
            finally:
                lock.unlink()
        """), encoding="utf-8")
        monitor = DetectedMonitor(2, 14, "DEL", "DELL U3011", "PH5NY2CIANXL")
        results = await asyncio.gather(
            self.client.read_brightness(monitor), self.client.detect(),
            return_exceptions=True,
        )
        self.assertEqual(results[0], (50, 100))
        self.assertEqual(results[1], parse_detect_output(DETECT_OUTPUT))

    async def test_temporary_file_failure_is_a_command_error(self):
        # Output-file setup failures must reach the worker as DdcCommandError.
        with patch(
            "cosmic_ddc_brightness.ddc.tempfile.TemporaryFile",
            side_effect=OSError("too many open files"),
        ):
            with self.assertRaisesRegex(DdcCommandError, "cannot run .*too many open files"):
                await self.client.version()


class MovingBusClient:
    def __init__(self):
        self.bus = 4
        self.first_read = True

    async def detect(self):
        return [DetectedMonitor(1, self.bus, "DEL", "U2720Q", "SERIAL")]

    async def read_brightness(self, monitor):
        if self.first_read:
            self.first_read = False
            self.bus = 12
            raise DdcCommandError("monitor disconnected")
        if monitor.bus != 12:
            raise DdcCommandError("old bus")
        return 60, 100

    async def write_brightness(self, monitor, value):
        if self.bus == 4:
            self.bus = 12
            raise DdcCommandError("monitor disconnected")
        if monitor.bus != 12:
            raise DdcCommandError("old bus")
        self.written = value


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_recovers_after_bus_number_changes(self):
        configured = (MonitorConfig("desk", "Desk", "DEL", "U2720Q", "SERIAL"),)
        manager = DdcManager(configured, MovingBusClient())
        await manager.refresh()
        self.assertEqual(manager.matches["desk"].bus, 4)
        self.assertEqual(await manager.read("desk"), (60, 100))
        self.assertEqual(manager.matches["desk"].bus, 12)

    async def test_write_recovers_after_bus_number_changes(self):
        configured = (MonitorConfig("desk", "Desk", "DEL", "U2720Q", "SERIAL"),)
        client = MovingBusClient()
        manager = DdcManager(configured, client)
        await manager.refresh()
        await manager.write("desk", 40)
        self.assertEqual(client.written, 40)
        self.assertEqual(manager.matches["desk"].bus, 12)


class ReusedBusClient:
    def __init__(self):
        self.detected = [DetectedMonitor(1, 4, "DEL", "U2720Q", "ORIGINAL")]
        self.detect_calls = 0
        self.writes = []
        self.reads = []
        self.detect_error = False

    async def detect(self):
        self.detect_calls += 1
        if self.detect_error:
            raise DdcCommandError("cannot read monitor identity")
        return list(self.detected)

    async def read_brightness(self, selected):
        monitor = next(item for item in self.detected if item.identity == selected.identity)
        self.reads.append(monitor.serial)
        return (60 if monitor.serial == "ORIGINAL" else 20), 100

    async def write_brightness(self, selected, value):
        monitor = next(
            (item for item in self.detected if item.identity == selected.identity), None
        )
        if monitor is None:
            raise DdcCommandError("display not found")
        self.writes.append((monitor.serial, value))


class PausedDetectClient(ReusedBusClient):
    def __init__(self):
        super().__init__()
        self.second_detect_started = asyncio.Event()
        self.resume_second_detect = asyncio.Event()

    async def detect(self):
        if self.detect_calls == 1:
            self.second_detect_started.set()
            await self.resume_second_detect.wait()
        return await super().detect()


class ReusedBusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        configured = (MonitorConfig("desk", "Desk", "DEL", "U2720Q", "ORIGINAL"),)
        self.client = ReusedBusClient()
        self.manager = DdcManager(configured, self.client)
        await self.manager.refresh()

    def swap(self):
        self.client.detected = [
            DetectedMonitor(1, 4, "DEL", "U2720Q", "REPLACEMENT"),
            DetectedMonitor(2, 12, "DEL", "U2720Q", "ORIGINAL"),
        ]

    async def test_write_follows_identity_when_old_bus_still_responds(self):
        self.swap()
        await self.manager.write("desk", 40)
        self.assertEqual(self.client.writes, [("ORIGINAL", 40)])

    async def test_fast_write_follows_identity_without_a_second_scan(self):
        manager = DdcManager(
            self.manager.monitors, self.client, scan_before_each_write=False
        )
        await manager.refresh()
        self.swap()
        await manager.write("desk", 40)
        self.assertEqual(self.client.detect_calls, 2)
        self.assertEqual(self.client.writes, [("ORIGINAL", 40)])

    async def test_fast_write_skips_disconnected_identity(self):
        manager = DdcManager(
            self.manager.monitors, self.client, scan_before_each_write=False
        )
        await manager.refresh()
        self.client.detected = [
            DetectedMonitor(1, 4, "DEL", "U2720Q", "REPLACEMENT")
        ]
        with self.assertRaises(DdcCommandError):
            await manager.write("desk", 40)
        self.assertEqual(self.client.writes, [])

    async def test_fast_mode_rejects_duplicate_identity_at_startup(self):
        self.client.detected.append(
            DetectedMonitor(2, 12, "DEL", "U2720Q", "ORIGINAL")
        )
        manager = DdcManager(
            self.manager.monitors, self.client, scan_before_each_write=False
        )
        with self.assertRaises(AmbiguousMonitorError):
            await manager.refresh()
        self.assertEqual(self.client.writes, [])

    async def test_read_follows_identity_when_old_bus_still_responds(self):
        self.swap()
        self.assertEqual(await self.manager.read("desk"), (60, 100))
        self.assertEqual(self.client.reads, ["ORIGINAL"])
        self.assertEqual(self.client.detect_calls, 1)

    async def test_overlapping_writes_share_one_fresh_detection(self):
        client = PausedDetectClient()
        manager = DdcManager(self.manager.monitors, client)
        await manager.refresh()
        first = asyncio.create_task(manager.write("desk", 40))
        second = None
        try:
            await asyncio.wait_for(client.second_detect_started.wait(), 1.0)
            second = asyncio.create_task(manager.write("desk", 45))
            await asyncio.sleep(0)
            client.resume_second_detect.set()
            await asyncio.gather(first, second)
        finally:
            client.resume_second_detect.set()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
        self.assertEqual(client.detect_calls, 2)
        self.assertEqual(client.writes, [("ORIGINAL", 40), ("ORIGINAL", 45)])

    async def test_consecutive_writes_recheck_identity_without_idle_delay(self):
        await self.manager.write("desk", 35)
        self.swap()
        await self.manager.write("desk", 40)
        self.assertEqual(self.client.writes, [("ORIGINAL", 35), ("ORIGINAL", 40)])

    async def test_disconnected_identity_never_writes_to_replacement(self):
        self.swap()
        self.client.detected = self.client.detected[:1]
        with self.assertRaises(DdcCommandError):
            await self.manager.write("desk", 40)
        self.assertEqual(self.client.writes, [])

    async def test_new_duplicate_identity_blocks_write(self):
        self.client.detected.append(DetectedMonitor(2, 12, "DEL", "U2720Q", "ORIGINAL"))
        with self.assertRaises(AmbiguousMonitorError):
            await self.manager.write("desk", 40)
        self.assertEqual(self.client.writes, [])

    async def test_detection_recovers_on_next_command(self):
        self.client.detect_error = True
        with self.assertRaises(DdcCommandError):
            await self.manager.write("desk", 40)
        self.assertEqual(self.client.writes, [])
        self.assertEqual(self.manager.matches, {})
        self.client.detect_error = False
        self.swap()
        await self.manager.write("desk", 45)
        self.assertEqual(self.client.writes, [("ORIGINAL", 45)])

    async def test_missing_detected_identity_never_writes(self):
        self.client.detected = [DetectedMonitor(1, 4, "DEL", "U2720Q", "")]
        with self.assertRaises(DdcCommandError):
            await self.manager.write("desk", 40)
        self.assertEqual(self.client.writes, [])


class IdentitySubprocessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sysfs = self.root / "sys"
        self.state = self.root / "topology.json"
        self.log = self.root / "commands.jsonl"
        self.original = {
            "bus": 4, "connector": "card0-DP-1", "serial": "ORIGINAL", "value": 60
        }
        self.moved = {
            "bus": 12, "connector": "card0-DP-2", "serial": "ORIGINAL", "value": 60
        }
        self.replacement = {
            "bus": 4, "connector": "card0-DP-1", "serial": "REPLACEMENT", "value": 20
        }
        self.set_topology([self.original])
        program = self.root / "ddcutil"
        program.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json
            import pathlib
            import sys
            state = pathlib.Path({str(self.state)!r})
            log = pathlib.Path({str(self.log)!r})
            data = json.loads(state.read_text())
            args = sys.argv[1:]
            if args == ["detect", "--brief"]:
                for index, monitor in enumerate(data["monitors"], 1):
                    print("Display " + str(index))
                    print("   I2C bus: /dev/i2c-" + str(monitor["bus"]))
                    print("   Monitor: DEL:MODEL SPACES:" + monitor["serial"])
                if "after_detect" in data:
                    data["monitors"] = data.pop("after_detect")
                    state.write_text(json.dumps(data))
                raise SystemExit(0)
            if args == ["detect", "--verbose"]:
                for index, monitor in enumerate(data["monitors"], 1):
                    print("Display " + str(index))
                    print("   I2C bus: /dev/i2c-" + str(monitor["bus"]))
                    print("   DRM_connector: " + monitor["connector"])
                raise SystemExit(0)
            # Fake ddcutil's EDID selectors and first match.
            if "--bus" in args:
                candidates = [m for m in data["monitors"]
                              if m["bus"] == int(args[args.index("--bus") + 1])]
            else:
                assert args[args.index("--mfg") + 1] == "DEL"
                assert args[args.index("--model") + 1] == "MODEL SPACES"
                serial = args[args.index("--sn") + 1]
                candidates = [m for m in data["monitors"] if m["serial"] == serial]
            if not candidates:
                print("Display not found", file=sys.stderr)
                raise SystemExit(1)
            selected = candidates[0]
            with log.open("a") as stream:
                stream.write(json.dumps({{"serial": selected["serial"], "args": args}}) + "\\n")
            if "getvcp" in args:
                print("VCP 10 C " + str(selected["value"]) + " 100")
            elif "setvcp" in args:
                selected["value"] = int(args[-1])
                state.write_text(json.dumps(data))
            else:
                raise SystemExit(2)
        """), encoding="utf-8")
        program.chmod(0o755)
        self.configured = (
            MonitorConfig("desk", "Desk", "DEL", "MODEL SPACES", "ORIGINAL"),
        )
        self.client = DdcClient(str(program), timeout=2.0, sysfs_root=self.sysfs)
        self.manager = DdcManager(self.configured, self.client)
        await self.manager.refresh()

    def set_topology(self, monitors, after_detect=None):
        data = {"monitors": monitors}
        if after_detect is not None:
            data["after_detect"] = after_detect
        self.state.write_text(json.dumps(data), encoding="utf-8")
        drm = self.sysfs / "class/drm"
        i2c = self.sysfs / "bus/i2c/devices"
        shutil.rmtree(drm, ignore_errors=True)
        shutil.rmtree(i2c, ignore_errors=True)
        for monitor in monitors:
            connector = drm / monitor["connector"]
            connector.mkdir(parents=True, exist_ok=True)
            (connector / "status").write_text("connected\n", encoding="utf-8")
            (connector / "edid").write_bytes(
                sample_edid(model="MODEL SPACES", serial=monitor["serial"])
            )
            (i2c / f"i2c-{monitor['bus']}").mkdir(parents=True, exist_ok=True)

    def assert_only_original_changed(self, expected_bus=None):
        monitors = json.loads(self.state.read_text())["monitors"]
        self.assertEqual({m["serial"]: m["value"] for m in monitors},
                         {"ORIGINAL": 40, "REPLACEMENT": 20})
        commands = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertTrue(commands)
        self.assertTrue(all(c["serial"] == "ORIGINAL" for c in commands))
        if expected_bus is None:
            self.assertTrue(all("--bus" not in c["args"] for c in commands))
        else:
            self.assertEqual(len(commands), 1)
            arguments = commands[0]["args"]
            self.assertEqual(arguments[arguments.index("--bus") + 1], str(expected_bus))

    async def test_reused_bus_through_real_command_flow(self):
        self.set_topology([self.replacement, self.moved])
        self.assertEqual(await self.manager.read("desk"), (60, 100))
        await self.manager.write("desk", 40)
        self.assert_only_original_changed()

    async def test_fast_write_rechecks_guard_and_uses_new_bus_after_move(self):
        manager = DdcManager(
            self.configured, self.client, scan_before_each_write=False
        )
        await manager.refresh()
        self.assertIn("desk", manager.bus_guards)
        self.set_topology([self.replacement, self.moved])
        await manager.write("desk", 40)
        self.assert_only_original_changed(expected_bus=12)

    async def test_fast_write_never_uses_old_bus_for_replacement(self):
        manager = DdcManager(
            self.configured, self.client, scan_before_each_write=False
        )
        await manager.refresh()
        self.set_topology([self.replacement])
        with self.assertRaisesRegex(DdcCommandError, "not connected"):
            await manager.write("desk", 40)
        self.assertFalse(self.log.exists())
        self.assertEqual(json.loads(self.state.read_text())["monitors"], [self.replacement])

    async def test_swap_between_detection_and_write_uses_identity(self):
        self.set_topology([self.original], [self.replacement, self.moved])
        await self.manager.write("desk", 40)
        self.assert_only_original_changed()

    async def test_disconnect_between_detection_and_write_changes_nothing(self):
        self.set_topology([self.original], [self.replacement])
        with self.assertRaises(DdcCommandError):
            await self.manager.write("desk", 40)
        self.assertFalse(self.log.exists())
        self.assertEqual(json.loads(self.state.read_text())["monitors"], [self.replacement])


def child_state(pid: int) -> str | None:
    """Read the Linux process state, or return None if it exited."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return stat.rpartition(")")[2].split()[0]


def kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def read_pid(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except FileNotFoundError:
        return None
    return int(text) if text else None


class CancelledCommandTests(unittest.IsolatedAsyncioTestCase):
    """Cancellation must stop and reap the DDC child."""

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.pid_file = self.root / "child.pid"
        self.grandchild_file = self.root / "grandchild.pid"
        self.marker = self.root / "signals.log"
        # Clean up test children after failed assertions too.
        self.addCleanup(self.kill_leftovers)
        program = self.root / "ddcutil"
        program.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import os
            import pathlib
            import signal
            import sys
            import time
            marker = pathlib.Path({str(self.marker)!r})

            def on_term(_signum, _frame):
                with marker.open("a") as stream:
                    stream.write("SIGTERM\\n")
                if "--ignore-term" not in sys.argv:
                    raise SystemExit(0)

            signal.signal(signal.SIGTERM, on_term)
            pathlib.Path({str(self.pid_file)!r}).write_text(str(os.getpid()))
            time.sleep(30)
        """), encoding="utf-8")
        program.chmod(0o755)
        self.client = DdcClient(str(program), timeout=20.0, stop_grace_seconds=0.3)

    def kill_leftovers(self):
        for path in (self.pid_file, self.grandchild_file):
            pid = read_pid(path)
            if pid is not None:
                kill_quietly(pid)

    async def finish_task(self, task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def wait_until(self, condition, description):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if condition():
                return
            await asyncio.sleep(0.01)
        self.fail(f"timed out waiting for {description}")

    async def wait_for_child(self):
        await self.wait_until(
            lambda: read_pid(self.pid_file) is not None
            and child_state(read_pid(self.pid_file)) is not None,
            "the fake ddcutil child to start",
        )
        return read_pid(self.pid_file)

    async def cancel_running_command(self, *arguments, client=None):
        client = client or self.client
        task = asyncio.create_task(client.command(*arguments))
        self.addAsyncCleanup(self.finish_task, task)
        pid = await self.wait_for_child()
        self.assertIsNotNone(child_state(pid))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(task.cancelled())
        return pid

    def client_with_output_holding_grandchild(self):
        # Leave a grandchild holding output descriptors, then exec the fake
        # ddcutil child.
        wrapper = self.root / "ddcutil-with-grandchild"
        wrapper.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            sleep 30 &
            echo $! > {str(self.grandchild_file)!r}
            exec {str(self.root / "ddcutil")!r} "$@"
        """), encoding="utf-8")
        wrapper.chmod(0o755)
        return DdcClient(str(wrapper), timeout=20.0, stop_grace_seconds=0.3)

    def assert_reaped(self, pid):
        # A zombie still has a /proc entry. A reaped child does not.
        self.assertIsNone(child_state(pid))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def assert_terminate_was_sent(self):
        await self.wait_until(self.marker.exists, "the SIGTERM marker")
        self.assertEqual(self.marker.read_text().split(), ["SIGTERM"])

    async def test_cancel_terminates_and_reaps_child(self):
        pid = await self.cancel_running_command("getvcp", "10")
        self.assert_reaped(pid)
        await self.assert_terminate_was_sent()

    async def test_cancel_kills_child_that_ignores_terminate(self):
        pid = await self.cancel_running_command("getvcp", "10", "--ignore-term")
        self.assert_reaped(pid)
        await self.assert_terminate_was_sent()

    async def test_grandchild_holding_output_does_not_block_cleanup(self):
        # SIGTERM stops the child, but the grandchild holds its output open.
        # Cancellation must finish and reap the direct child.
        client = self.client_with_output_holding_grandchild()
        pid = await self.cancel_running_command("getvcp", "10", client=client)
        self.assertIsNotNone(child_state(read_pid(self.grandchild_file)))
        self.assert_reaped(pid)
        await self.assert_terminate_was_sent()

    async def test_grandchild_holding_output_does_not_block_kill_path(self):
        client = self.client_with_output_holding_grandchild()
        pid = await self.cancel_running_command(
            "getvcp", "10", "--ignore-term", client=client
        )
        self.assertIsNotNone(child_state(read_pid(self.grandchild_file)))
        self.assert_reaped(pid)
        await self.assert_terminate_was_sent()

    async def test_timeout_is_bounded_when_grandchild_holds_output_open(self):
        client = self.client_with_output_holding_grandchild()
        client.timeout = 0.2
        task = asyncio.create_task(client.command("getvcp", "10", "--ignore-term"))
        self.addAsyncCleanup(self.finish_task, task)
        pid = await self.wait_for_child()
        started = time.monotonic()
        with self.assertRaisesRegex(DdcCommandError, "timed out after 0.2 seconds"):
            await task
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertIsNotNone(child_state(read_pid(self.grandchild_file)))
        self.assert_reaped(pid)

    async def test_child_exit_is_not_blocked_by_grandchild_holding_output_open(self):
        # A grandchild holds output open after the direct child exits.
        program = self.root / "exits-early"
        program.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            sleep 30 &
            echo $! > {str(self.grandchild_file)!r}
            exit 0
        """), encoding="utf-8")
        program.chmod(0o755)
        created = []
        original = asyncio.create_subprocess_exec

        async def capture(*arguments, **options):
            process = await original(*arguments, **options)
            created.append(process)
            return process

        client = DdcClient(str(program), timeout=20.0, stop_grace_seconds=0.3)
        with patch("cosmic_ddc_brightness.ddc.asyncio.create_subprocess_exec", capture):
            self.assertEqual(
                await asyncio.wait_for(client.command("getvcp", "10"), 1.0), ""
            )
        self.assertIsNotNone(child_state(read_pid(self.grandchild_file)))
        self.assert_reaped(created[0].pid)

class StubProcess:
    """Process stub with scripted signal behavior."""

    def __init__(self, exit_before_terminate=False, block_first_wait=False):
        self.returncode = None
        self.calls = []
        self.started = asyncio.Event()
        self.exit_before_terminate = exit_before_terminate
        self.block_first_wait = block_first_wait

    async def communicate(self):
        self.started.set()
        await asyncio.Event().wait()

    def terminate(self):
        self.calls.append("terminate")
        if self.exit_before_terminate:
            raise ProcessLookupError

    def kill(self):
        self.calls.append("kill")

    async def wait(self):
        self.calls.append("wait")
        if self.block_first_wait and self.calls.count("wait") == 1:
            await asyncio.Event().wait()
        self.returncode = 0
        return 0


class CancelledCommandRaceTests(unittest.IsolatedAsyncioTestCase):
    async def run_cancelled(self, stub, *, cancel_again=False):
        client = DdcClient("stub", timeout=20.0, stop_grace_seconds=5.0)
        with patch(
            "cosmic_ddc_brightness.ddc.asyncio.create_subprocess_exec",
            AsyncMock(return_value=stub),
        ):
            task = asyncio.create_task(client.command("getvcp", "10"))
            await stub.started.wait()
            task.cancel()
            if cancel_again:
                for _ in range(100):
                    if "wait" in stub.calls:
                        break
                    await asyncio.sleep(0)
                self.assertIn("wait", stub.calls)
                task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(task.cancelled())

    async def test_child_exiting_as_cleanup_starts_is_still_reaped(self):
        stub = StubProcess(exit_before_terminate=True)
        await self.run_cancelled(stub)
        self.assertEqual(stub.calls, ["terminate", "wait"])

    async def test_second_cancel_during_cleanup_kills_and_reaps(self):
        stub = StubProcess(block_first_wait=True)
        await self.run_cancelled(stub, cancel_again=True)
        self.assertEqual(stub.calls, ["terminate", "wait", "kill", "wait"])
        self.assertEqual(stub.returncode, 0)


if __name__ == "__main__":
    unittest.main()
