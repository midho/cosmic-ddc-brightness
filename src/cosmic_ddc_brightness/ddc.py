from __future__ import annotations

import asyncio
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import MonitorConfig
from .errors import DeviceCommandError


@dataclass(frozen=True)
class DetectedMonitor:
    display_number: int
    bus: int
    manufacturer: str
    model: str
    serial: str

    @property
    def identity(self) -> str:
        return f"{self.manufacturer}:{self.model}:{self.serial}"


class DdcCommandError(DeviceCommandError):
    pass


class AmbiguousMonitorError(DdcCommandError):
    pass


class DisconnectedMonitorError(DdcCommandError):
    """The configured identity is absent from the connected outputs."""


DISPLAY_PATTERN = re.compile(r"(?m)^Display\s+(\d+)\s*$")
BUS_PATTERN = re.compile(r"(?m)^\s*I2C bus:\s*/dev/i2c-(\d+)\s*$")
MONITOR_PATTERN = re.compile(r"(?m)^\s*Monitor:\s*(.*?)\s*$")
CONNECTOR_PATTERN = re.compile(r"(?m)^\s*DRM_connector:\s*(card\d+-[A-Za-z0-9_-]+)\s*$")
BRIGHTNESS_PATTERN = re.compile(r"(?m)^VCP\s+10\s+C\s+(\d+)\s+(\d+)\s*$")


def parse_detect_output(output: str) -> list[DetectedMonitor]:
    starts = list(DISPLAY_PATTERN.finditer(output))
    monitors: list[DetectedMonitor] = []
    for index, display_match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(output)
        block = output[display_match.end():end]
        bus_match = BUS_PATTERN.search(block)
        monitor_match = MONITOR_PATTERN.search(block)
        if not bus_match or not monitor_match:
            continue
        identity = monitor_match.group(1).split(":", 2)
        if len(identity) != 3:
            continue
        manufacturer, model, serial = (part.strip() for part in identity)
        if not manufacturer or not model:
            continue
        monitors.append(DetectedMonitor(
            display_number=int(display_match.group(1)),
            bus=int(bus_match.group(1)),
            manufacturer=manufacturer.upper(),
            model=model,
            serial=serial,
        ))
    return monitors


def parse_connector_output(output: str) -> dict[int, str]:
    starts = list(DISPLAY_PATTERN.finditer(output))
    connectors: dict[int, str] = {}
    duplicate_buses: set[int] = set()
    for index, display_match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(output)
        block = output[display_match.end():end]
        bus = BUS_PATTERN.search(block)
        connector = CONNECTOR_PATTERN.search(block)
        if bus and connector:
            number = int(bus.group(1))
            if number in connectors:
                connectors.pop(number)
                duplicate_buses.add(number)
            elif number not in duplicate_buses:
                connectors[number] = connector.group(1)
    return connectors


def edid_identity(edid: bytes) -> tuple[str, str, str] | None:
    if len(edid) < 128 or edid[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None
    if sum(edid[:128]) % 256:
        return None
    code = int.from_bytes(edid[8:10], "big")
    manufacturer = "".join(chr(64 + ((code >> shift) & 31)) for shift in (10, 5, 0))
    if not manufacturer.isalpha():
        return None
    descriptors: dict[int, str] = {}
    for start in (54, 72, 90, 108):
        item = edid[start:start + 18]
        if item[:3] == b"\x00\x00\x00" and item[3] in (0xFC, 0xFF):
            descriptors[item[3]] = item[5:18].decode("ascii", errors="replace").strip("\x00\n ")
    model = descriptors.get(0xFC, "")
    serial = descriptors.get(0xFF, "")
    return (manufacturer, model, serial) if model and serial else None


@dataclass(frozen=True)
class BusGuard:
    bus: int
    connector: str
    edid: bytes
    bus_path: Path


def parse_brightness(output: str) -> tuple[int, int] | None:
    match = BRIGHTNESS_PATTERN.search(output.strip())
    if not match:
        return None
    current, maximum = map(int, match.groups())
    if maximum <= 0 or current < 0 or current > maximum:
        return None
    return current, maximum


def _same(value: str, expected: str) -> bool:
    return value.strip().casefold() == expected.strip().casefold()


def matching_candidates(
    configured: MonitorConfig, detected: list[DetectedMonitor]
) -> list[DetectedMonitor]:
    return [
        monitor for monitor in detected
        if _same(monitor.manufacturer, configured.manufacturer)
        and _same(monitor.model, configured.model)
        and (not configured.serial or _same(monitor.serial, configured.serial))
    ]


def match_monitors(
    configured: tuple[MonitorConfig, ...], detected: list[DetectedMonitor]
) -> dict[str, DetectedMonitor]:
    matches: dict[str, DetectedMonitor] = {}
    used_buses: set[int] = set()
    for wanted in configured:
        if not wanted.serial.strip():
            raise DdcCommandError(
                f"{wanted.name!r} has no configured EDID serial number. "
                "It cannot be distinguished from a replacement of the same model"
            )
        candidates = matching_candidates(wanted, detected)
        if len(candidates) > 1:
            raise AmbiguousMonitorError(
                f"{wanted.name!r} matches more than one connected monitor. Add its EDID serial to the configuration"
            )
        if not candidates:
            continue
        candidate = candidates[0]
        if candidate.bus in used_buses:
            raise AmbiguousMonitorError(
                f"more than one configured monitor matches {candidate.identity}"
            )
        matches[wanted.id] = candidate
        used_buses.add(candidate.bus)
    return matches


def _signal_child(action) -> None:
    try:
        action()
    except ProcessLookupError:
        pass  # The child exited just before the signal was sent.


class DdcClient:
    def __init__(
        self,
        executable: str = "ddcutil",
        timeout: float = 3.0,
        stop_grace_seconds: float = 0.5,
        sysfs_root: Path = Path("/sys"),
    ):
        self.executable = executable
        self.timeout = timeout
        self.stop_grace_seconds = stop_grace_seconds
        self.sysfs_root = sysfs_root
        self._command_lock = asyncio.Lock()

    async def command(self, *arguments: str, guard: BusGuard | None = None) -> str:
        # Detection also opens connected monitors. Serialize it with reads and
        # writes so our own commands cannot make a working monitor look busy.
        async with self._command_lock:
            if guard is not None and not self._guard_is_current(guard):
                raise DdcCommandError("monitor identity or bus changed before the command")
            return await self._command(*arguments)

    async def _command(self, *arguments: str) -> str:
        process = None
        try:
            # A grandchild can keep a pipe open after ddcutil exits. Temporary
            # files avoid that wait and close when this command returns.
            with (
                tempfile.TemporaryFile() as output_stream,
                tempfile.TemporaryFile() as error_stream,
            ):
                process = await asyncio.create_subprocess_exec(
                    self.executable,
                    *arguments,
                    stdout=output_stream,
                    stderr=error_stream,
                )
                await asyncio.wait_for(process.communicate(), self.timeout)
                output_stream.seek(0)
                error_stream.seek(0)
                output = output_stream.read()
                error = error_stream.read()
        except asyncio.TimeoutError as error:
            if process is not None:
                await self._stop_after_timeout(process)
            raise DdcCommandError(
                f"ddcutil timed out after {self.timeout:.1f} seconds"
            ) from error
        except asyncio.CancelledError:
            # Stop and reap ddcutil before passing cancellation to the caller.
            if process is not None:
                await self._stop_after_cancel(process)
            raise
        except OSError as error:
            raise DdcCommandError(f"cannot run {self.executable}: {error}") from error

        if process.returncode != 0:
            detail = (error or output).decode(errors="replace").strip()
            raise DdcCommandError(detail or f"ddcutil exited with status {process.returncode}")
        return output.decode(errors="replace")

    async def _stop_after_timeout(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            _signal_child(process.kill)
        await self._wait_for_exit(process)

    async def _stop_after_cancel(self, process: asyncio.subprocess.Process) -> None:
        # Give SIGTERM a short grace period. SIGKILL follows if needed.
        try:
            if process.returncode is None:
                _signal_child(process.terminate)
                if await self._wait_for_exit(process):
                    return
                _signal_child(process.kill)
            await self._wait_for_exit(process)
        except asyncio.CancelledError:
            # A second cancellation still needs a reaped child.
            _signal_child(process.kill)
            await self._wait_for_exit(process)

    async def _wait_for_exit(self, process: asyncio.subprocess.Process) -> bool:
        # The child watcher may lag, so cap this wait.
        try:
            await asyncio.wait_for(process.wait(), self.stop_grace_seconds)
        except asyncio.TimeoutError:
            pass
        return process.returncode is not None

    async def version(self) -> str:
        output = await self.command("--version")
        return output.splitlines()[0] if output else "ddcutil"

    async def detect(self) -> list[DetectedMonitor]:
        return parse_detect_output(await self.command("detect", "--brief"))

    async def detect_connectors(self) -> dict[int, str]:
        return parse_connector_output(await self.command("detect", "--verbose"))

    def connector_state(self) -> tuple[tuple[str, str, bytes], ...] | None:
        # Read the kernel's connection records without probing DDC controls.
        # An incomplete snapshot cannot prove a saved monitor is unplugged.
        root = self.sysfs_root / "class/drm"
        try:
            if not root.is_dir():
                return None
            outputs = []
            for connector in sorted(root.glob("card*-*")):
                if not re.fullmatch(r"card\d+-[A-Za-z0-9_-]+", connector.name):
                    continue
                status = (connector / "status").read_text().strip()
                if status not in {"connected", "disconnected"}:
                    return None
                edid = (connector / "edid").read_bytes() if status == "connected" else b""
                if status == "connected" and edid_identity(edid) is None:
                    return None
                outputs.append((connector.name, status, edid))
            return tuple(outputs) if outputs else None
        except OSError:
            return None

    def prepare_bus_guard(self, monitor: DetectedMonitor, connector: str) -> BusGuard | None:
        if not re.fullmatch(r"card\d+-[A-Za-z0-9_-]+", connector):
            return None
        try:
            edid = (self.sysfs_root / "class/drm" / connector / "edid").read_bytes()
            status = (self.sysfs_root / "class/drm" / connector / "status").read_text().strip()
            bus_path = (self.sysfs_root / "bus/i2c/devices" / f"i2c-{monitor.bus}").resolve(strict=True)
        except OSError:
            return None
        identity = edid_identity(edid)
        if status != "connected" or identity is None:
            return None
        if tuple(part.casefold() for part in identity) != tuple(
            part.casefold() for part in (monitor.manufacturer, monitor.model, monitor.serial)
        ):
            return None
        return BusGuard(monitor.bus, connector, edid, bus_path)

    def _guard_is_current(self, guard: BusGuard) -> bool:
        try:
            connector = self.sysfs_root / "class/drm" / guard.connector
            return (
                (connector / "status").read_text().strip() == "connected"
                and (connector / "edid").read_bytes() == guard.edid
                and (self.sysfs_root / "bus/i2c/devices" / f"i2c-{guard.bus}").resolve(strict=True)
                == guard.bus_path
            )
        except OSError:
            return False

    async def write_brightness_bus(self, guard: BusGuard, value: int) -> None:
        if not self._guard_is_current(guard):
            raise DdcCommandError("monitor identity or bus changed before the write")
        await self.command(
            "--bus", str(guard.bus), "--skip-ddc-checks",
            "--disable-dynamic-sleep", "--sleep-multiplier", "1", "--noverify",
            "setvcp", "10", str(value), guard=guard,
        )

    async def read_brightness_bus(self, guard: BusGuard) -> tuple[int, int]:
        if not self._guard_is_current(guard):
            raise DdcCommandError("monitor identity or bus changed before the read")
        output = await self.command(
            "--bus", str(guard.bus), "--skip-ddc-checks",
            "--disable-dynamic-sleep", "--sleep-multiplier", "1",
            "--brief", "getvcp", "10", guard=guard,
        )
        result = parse_brightness(output)
        if result is None:
            raise DdcCommandError(f"unexpected brightness output for {guard.connector}: {output.strip()}")
        return result

    @staticmethod
    def _selector(monitor: DetectedMonitor) -> tuple[str, ...]:
        if not monitor.manufacturer or not monitor.model or not monitor.serial:
            raise DdcCommandError(
                f"monitor {monitor.model!r} has no complete EDID identity with a serial number. "
                "It cannot be selected safely for brightness control"
            )
        # Select by EDID inside the command. A bus may change after detect.
        return (
            "--mfg", monitor.manufacturer, "--model", monitor.model,
            "--sn", monitor.serial,
        )

    async def read_brightness(self, monitor: DetectedMonitor) -> tuple[int, int]:
        output = await self.command(
            *self._selector(monitor), "--skip-ddc-checks",
            "--disable-dynamic-sleep", "--sleep-multiplier", "1",
            "--brief", "getvcp", "10"
        )
        result = parse_brightness(output)
        if result is None:
            raise DdcCommandError(f"unexpected brightness output for {monitor.identity}: {output.strip()}")
        return result

    async def write_brightness(self, monitor: DetectedMonitor, value: int) -> None:
        await self.command(
            *self._selector(monitor), "--skip-ddc-checks",
            "--disable-dynamic-sleep", "--sleep-multiplier", "1", "--noverify",
            "setvcp", "10", str(value),
        )


class DdcManager:
    def __init__(
        self,
        monitors: tuple[MonitorConfig, ...],
        client: DdcClient,
        *,
        scan_before_each_write: bool = True,
    ):
        self.monitors = monitors
        self.client = client
        self.scan_before_each_write = scan_before_each_write
        self.matches: dict[str, DetectedMonitor] = {}
        self.bus_guards: dict[str, BusGuard] = {}
        self._detect_lock = asyncio.Lock()
        self._detect_generation = 0
        self._detect_error: Exception | None = None
        self._detected_outputs: tuple[tuple[str, str, bytes], ...] | None = None

    async def refresh(self) -> dict[str, DetectedMonitor]:
        generation = self._detect_generation
        async with self._detect_lock:
            # Workers that asked during the same scan can use its result.
            # A later call starts a new scan, including on the next keypress.
            if generation != self._detect_generation:
                if self._detect_error is not None:
                    raise self._detect_error
                return dict(self.matches)
            # Drop the old mapping when detection fails or finds duplicates.
            self.matches = {}
            self.bus_guards = {}
            self._detected_outputs = None
            outputs = self.client.connector_state() if isinstance(self.client, DdcClient) else None
            try:
                detected = await self.client.detect()
                self.matches = match_monitors(self.monitors, detected)
                if not self.scan_before_each_write and isinstance(self.client, DdcClient):
                    try:
                        connectors = await self.client.detect_connectors()
                    except DdcCommandError:
                        connectors = {}
                    for monitor_id, monitor in self.matches.items():
                        connector = connectors.get(monitor.bus)
                        if connector is not None:
                            guard = self.client.prepare_bus_guard(monitor, connector)
                            if guard is not None:
                                self.bus_guards[monitor_id] = guard
            except Exception as error:
                self._detect_error = error
                self._detect_generation += 1
                raise
            self._detected_outputs = outputs
            self._detect_error = None
            self._detect_generation += 1
            return dict(self.matches)

    def _known_disconnected(self, monitor_id: str) -> bool:
        if not isinstance(self.client, DdcClient) or self._detected_outputs is None:
            return False
        current = self.client.connector_state()
        if current is None or current != self._detected_outputs:
            return False
        wanted = next((item for item in self.monitors if item.id == monitor_id), None)
        if wanted is None:
            return False
        expected = tuple(part.strip().casefold() for part in (wanted.manufacturer, wanted.model, wanted.serial))
        for _connector, status, edid in current:
            if status == "connected":
                identity = edid_identity(edid)
                if identity is None or tuple(part.strip().casefold() for part in identity) == expected:
                    return False
        return True

    async def _monitor(self, monitor_id: str) -> DetectedMonitor:
        # Writes need a fresh scan. Another worker may refresh detection too,
        # so keep this call's snapshot.
        if monitor_id not in self.matches and self._known_disconnected(monitor_id):
            raise DisconnectedMonitorError(f"configured monitor {monitor_id!r} is not connected")
        matches = await self.refresh()
        match = matches.get(monitor_id)
        if match is None:
            error = DisconnectedMonitorError if self._known_disconnected(monitor_id) else DdcCommandError
            raise error(f"configured monitor {monitor_id!r} is not connected")
        return match

    async def _read_matched(self, monitor_id: str, monitor: DetectedMonitor) -> tuple[int, int]:
        guard = self.bus_guards.get(monitor_id)
        if guard is not None and guard.bus == monitor.bus:
            return await self.client.read_brightness_bus(guard)
        return await self.client.read_brightness(monitor)

    async def read(self, monitor_id: str) -> tuple[int, int]:
        # Fast reads reuse the same identity guard as fast writes. Otherwise
        # ddcutil selects by EDID. A successful read needs no extra scan.
        monitor = self.matches.get(monitor_id)
        if monitor is None:
            monitor = await self._monitor(monitor_id)
        try:
            return await self._read_matched(monitor_id, monitor)
        except DdcCommandError:
            current = await self._monitor(monitor_id)
            if current.bus == monitor.bus:
                raise
            return await self._read_matched(monitor_id, current)

    async def write(self, monitor_id: str, value: int) -> None:
        if self.scan_before_each_write:
            monitor = await self._monitor(monitor_id)
        else:
            monitor = self.matches.get(monitor_id)
            if monitor is None:
                monitor = await self._monitor(monitor_id)
        try:
            guard = self.bus_guards.get(monitor_id)
            if guard is not None and guard.bus == monitor.bus:
                await self.client.write_brightness_bus(guard, value)
            else:
                await self.client.write_brightness(monitor, value)
        except DdcCommandError:
            current = await self._monitor(monitor_id)
            if current.bus == monitor.bus:
                raise
            guard = self.bus_guards.get(monitor_id)
            if guard is not None and guard.bus == current.bus:
                await self.client.write_brightness_bus(guard, value)
            else:
                await self.client.write_brightness(current, value)


def slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return (cleaned or "monitor")[:32].rstrip("-")


def configurations_from_detected(detected: list[DetectedMonitor]) -> list[MonitorConfig]:
    identities: set[tuple[str, str, str]] = set()
    used_ids: set[str] = set()
    result: list[MonitorConfig] = []
    for monitor in detected:
        if not monitor.serial.strip():
            raise DdcCommandError(
                f"{monitor.model!r} has no readable EDID serial number. "
                "Brightness control cannot be configured safely"
            )
        identity = (
            monitor.manufacturer.casefold(),
            monitor.model.casefold(),
            monitor.serial.casefold(),
        )
        if identity in identities:
            raise AmbiguousMonitorError(
                f"two detected monitors have the same identity {monitor.identity}. The configuration would be ambiguous"
            )
        identities.add(identity)
        base = slug(f"{monitor.manufacturer}-{monitor.model}")
        monitor_id = base
        suffix = 2
        while monitor_id in used_ids:
            tail = f"-{suffix}"
            monitor_id = f"{base[:32-len(tail)]}{tail}"
            suffix += 1
        used_ids.add(monitor_id)
        result.append(MonitorConfig(
            id=monitor_id,
            name=monitor.model,
            manufacturer=monitor.manufacturer,
            model=monitor.model,
            serial=monitor.serial,
        ))
    return result
