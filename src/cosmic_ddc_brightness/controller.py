from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from .config import DeviceConfig, GeekMagicConfig, Settings
from .errors import DeviceCommandError
from .ddc import DisconnectedMonitorError


RETRY_DELAYS = (0.2, 0.5, 1.0)
MAX_FAILURES = len(RETRY_DELAYS) + 1
CHORD_WINDOW_SECONDS = 0.08
CHORD_RELEASE_SECONDS = 0.35


def brightness_step(current: int, maximum: int, delta_percent: float) -> int:
    if maximum <= 0:
        raise ValueError("maximum brightness must be positive")
    current_percent = 100.0 * current / maximum
    target_percent = max(0.0, min(100.0, current_percent + delta_percent))
    target = max(0, min(maximum, round(maximum * target_percent / 100.0)))
    if delta_percent > 0 and current < maximum:
        return max(current + 1, target)
    if delta_percent < 0 and current > 0:
        return min(current - 1, target)
    return target


def device_brightness_step(
    config: DeviceConfig,
    current: int,
    maximum: int,
    delta_percent: float,
) -> int:
    if isinstance(config, GeekMagicConfig):
        # Zero is reserved for the explicit off/on toggle. Brightness keys do
        # not wake an off display, and the lowest visible level is one percent.
        if current == 0:
            return 0
        if delta_percent > 0 and current < 5:
            return min(maximum, 5)
        return max(1, brightness_step(current, maximum, delta_percent))
    return brightness_step(current, maximum, delta_percent)


class Popup(Protocol):
    async def publish(self, message: dict) -> None: ...
    async def close(self) -> None: ...
    def set_wayland_display(self, display: str) -> None: ...


class Manager(Protocol):
    async def read(self, device_id: str) -> tuple[int, int]: ...
    async def write(self, device_id: str, value: int) -> None: ...
    def toggle_target(self, device_id: str, current: int) -> int: ...


class CommandState:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pending_percent = 0.0
        self.changed = asyncio.Event()
        self.held: int | None = None
        self.next_repeat = 0.0
        self.hold_until = 0.0
        self.show_requested = False
        self.cycle_requested = 0
        self.toggle_geekmagic_requested = 0
        self.pending_directions: set[int] = set()
        self.chord_latched = False
        self.last_direction_at = 0.0

    def queue_direction(self, direction: int, now: float) -> None:
        self.last_direction_at = now
        if self.chord_latched:
            return
        self.pending_directions.add(direction)
        self.pending_percent = max(
            -100.0,
            min(100.0, self.pending_percent + direction * self.settings.step_percent),
        )

    def handle(self, action: str, now: float) -> None:
        if action in {"up", "down"}:
            direction = 1 if action == "up" else -1
            self.queue_direction(direction, now)
        elif action in {"press-up", "press-down"}:
            direction = 1 if action == "press-up" else -1
            if self.chord_latched:
                return
            if self.held == direction:
                return
            self.held = direction
            self.next_repeat = now + self.settings.hold_delay_seconds
            self.hold_until = now + self.settings.hold_limit_seconds
            self.queue_direction(direction, now)
        elif action in {"release-up", "release-down"}:
            direction = 1 if action == "release-up" else -1
            if self.held == direction:
                self.held = None
        elif action == "stop":
            self.held = None
        elif action == "show":
            self.show_requested = True
        elif action == "cycle-monitor":
            self.cycle_requested += 1
        elif action == "toggle-geekmagic":
            self.toggle_geekmagic_requested += 1
        else:
            return
        self.changed.set()

    def repeat_delay(self, now: float) -> float | None:
        if self.held is None or now >= self.hold_until:
            return None
        if self.next_repeat >= self.hold_until:
            return None
        return max(0.0, self.next_repeat - now)


@dataclass
class MonitorState:
    config: DeviceConfig
    maximum: int | None = None
    observed: int | None = None
    applied: int | None = None
    target: int | None = None
    pending_percent: float = 0.0
    version: int = 0
    active: bool = False
    dirty: bool = False
    check_requested: bool = True
    check_after: float = 0.0
    last_observed_at: float | None = None
    failures: int = 0
    retry_at: float = 0.0
    blocked: bool = False
    status: str = "Reading…"
    toggle_requests: int = 0

    def __post_init__(self) -> None:
        self.changed = asyncio.Event()


class BrightnessController:
    def __init__(self, settings: Settings, manager: Manager, popup: Popup | None = None):
        self.settings = settings
        self.manager = manager
        self.popup = popup
        self.monitors = {item.id: MonitorState(item) for item in settings.devices}
        self.selection: str | None = None
        self.tasks: list[asyncio.Task] = []

    async def run(self) -> None:
        self.tasks = [
            asyncio.create_task(self.worker(monitor), name=f"monitor-{monitor.config.id}")
            for monitor in self.monitors.values()
        ]
        try:
            await asyncio.gather(*self.tasks)
        except asyncio.CancelledError:
            # Wait for workers to stop their DDC children before exiting.
            await asyncio.gather(*self.tasks, return_exceptions=True)
            raise
        except BaseException:
            # An unexpected worker failure stops the other workers too.
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            raise

    def selected_monitors(self) -> tuple[MonitorState, ...]:
        if self.selection is None:
            return tuple(self.monitors.values())
        return (self.monitors[self.selection],)

    async def apply(self, delta_percent: float) -> None:
        now = asyncio.get_running_loop().time()
        for monitor in self.selected_monitors():
            stale = (
                monitor.last_observed_at is None
                or (
                    self.settings.refresh_after_seconds > 0
                    and now - monitor.last_observed_at >= self.settings.refresh_after_seconds
                )
            )
            # Keep presses received during a read. Apply them to its result,
            # and reread a blocked target because that write was unconfirmed.
            if (
                monitor.maximum is None
                or monitor.target is None
                or monitor.blocked
                or (not monitor.active and stale)
            ):
                monitor.target = None
                monitor.pending_percent = max(
                    -100.0, min(100.0, monitor.pending_percent + delta_percent)
                )
                monitor.dirty = False
                monitor.check_requested = True
                monitor.status = "Reading…"
            else:
                monitor.target = device_brightness_step(
                    monitor.config, monitor.target, monitor.maximum, delta_percent
                )
                monitor.dirty = True
                monitor.check_requested = False
                monitor.status = "Updating…"
            monitor.version += 1
            monitor.active = True
            monitor.failures = 0
            monitor.blocked = False
            monitor.retry_at = 0.0
            monitor.changed.set()
        await self.publish(True)

    async def cycle_target(self, steps: int = 1) -> None:
        choices: tuple[str | None, ...] = (None, *self.monitors.keys())
        index = choices.index(self.selection)
        self.selection = choices[(index + steps) % len(choices)]
        print(f"Brightness control: {self.selection_label()}.", flush=True)
        await self.publish(True)

    def selection_label(self) -> str:
        if self.selection is None:
            return "All displays"
        return self.monitors[self.selection].config.name

    async def toggle_geekmagic(self, requests: int = 1) -> None:
        for config in self.settings.geekmagic:
            monitor = self.monitors[config.id]
            monitor.toggle_requests += requests
            monitor.target = None
            monitor.maximum = None
            monitor.dirty = False
            monitor.check_requested = True
            monitor.check_after = 0.0
            monitor.version += 1
            monitor.active = True
            monitor.failures = 0
            monitor.blocked = False
            monitor.retry_at = 0.0
            monitor.status = "Reading…"
            monitor.changed.set()
        if not self.settings.geekmagic:
            print("No GeekMagic display is configured.", flush=True)
        await self.publish(True)

    async def show(self) -> None:
        for monitor in self.monitors.values():
            if not monitor.active:
                monitor.target = None
                monitor.maximum = None
                monitor.blocked = False
                monitor.failures = 0
                monitor.retry_at = 0.0
                monitor.check_requested = True
                monitor.check_after = 0.0
                monitor.status = "Reading…"
                monitor.changed.set()
        await self.publish(True)

    def snapshot(self, show: bool) -> dict:
        def displayed_percent(monitor: MonitorState) -> int | None:
            if not monitor.maximum:
                return None
            # Do not advance the bar to a queued target before the hardware
            # write finishes. It remains approximate until a read confirms it.
            value = monitor.applied if monitor.applied is not None else monitor.observed
            if value is None:
                return None
            return round(100 * value / monitor.maximum)

        return {
            "show": show,
            "title": f"Brightness · {self.selection_label()}",
            "levels": [
                {
                    "name": monitor.config.name,
                    "percent": displayed_percent(monitor),
                    "estimated": monitor.active or monitor.check_requested,
                    "status": monitor.status,
                    "selected": self.selection is None or monitor.config.id == self.selection,
                }
                for monitor in self.monitors.values()
            ],
        }

    async def publish(self, show: bool) -> None:
        if self.popup is None:
            return
        try:
            await self.popup.publish(self.snapshot(show))
        except Exception as error:
            print(f"Optional brightness popup failed: {error}", flush=True)

    def failed(self, monitor: MonitorState, reason: str | DeviceCommandError) -> None:
        if isinstance(reason, DisconnectedMonitorError):
            monitor.blocked = True
            monitor.active = False
            monitor.check_requested = False
            monitor.pending_percent = 0.0
            monitor.maximum = monitor.observed = monitor.applied = monitor.target = None
            monitor.status = "Unavailable"
            print(f"{monitor.config.name}: {reason}.", flush=True)
            return
        monitor.failures += 1
        if monitor.failures >= MAX_FAILURES:
            monitor.blocked = True
            monitor.status = "Not confirmed" if monitor.target is not None else "Unavailable"
            print(
                f"{monitor.config.name}: {reason}. Recovery paused until the next command.",
                flush=True,
            )
        else:
            monitor.status = "Updating…" if monitor.target is not None else "Reading…"
            monitor.retry_at = (
                asyncio.get_running_loop().time() + RETRY_DELAYS[monitor.failures - 1]
            )
            print(
                f"{monitor.config.name}: {reason}. Retry {monitor.failures}/{MAX_FAILURES - 1}.",
                flush=True,
            )

    async def check(self, monitor: MonitorState) -> None:
        version = monitor.version
        try:
            observed, maximum = await self.manager.read(monitor.config.id)
        except DeviceCommandError as error:
            if version == monitor.version:
                self.failed(monitor, error)
            return

        # A held key can change the target while this read is running.
        # Keep that newer target and let the worker write it next.
        if monitor.target is not None and version != monitor.version:
            return
        monitor.observed = observed
        monitor.applied = observed
        monitor.maximum = maximum
        monitor.last_observed_at = asyncio.get_running_loop().time()
        if monitor.target is None:
            target = observed
            toggle_requests = monitor.toggle_requests
            monitor.toggle_requests = 0
            try:
                for _ in range(toggle_requests):
                    target = self.manager.toggle_target(monitor.config.id, target)
            except DeviceCommandError as error:
                if version == monitor.version:
                    self.failed(monitor, error)
                return
            target = device_brightness_step(
                monitor.config, target, maximum, monitor.pending_percent
            )
            had_pending = monitor.pending_percent != 0 or toggle_requests != 0
            monitor.pending_percent = 0.0
            monitor.target = target
            monitor.dirty = had_pending and target != observed
            monitor.active = monitor.dirty
            monitor.check_requested = False
            monitor.failures = 0
            monitor.blocked = False
            monitor.status = "Updating…" if monitor.dirty else ""
            return

        if observed == monitor.target:
            was_active = monitor.active
            monitor.dirty = False
            monitor.active = False
            monitor.check_requested = False
            monitor.failures = 0
            monitor.blocked = False
            monitor.status = ""
            if was_active:
                print(
                    f"{monitor.config.name} confirmed at {observed}/{maximum}.", flush=True
                )
        else:
            monitor.dirty = True
            monitor.check_requested = False
            self.failed(monitor, f"reported {observed}, expected {monitor.target}")

    async def write(self, monitor: MonitorState) -> None:
        version = monitor.version
        target = monitor.target
        if target is None:
            return
        try:
            await self.manager.write(monitor.config.id, target)
        except DeviceCommandError as error:
            if version == monitor.version:
                self.failed(monitor, error)
            return
        # A newer request does not undo the write that just finished. Keep
        # that value visible while the worker moves on to the newer target.
        monitor.applied = target
        if version != monitor.version:
            return
        monitor.dirty = False
        monitor.check_requested = True
        monitor.check_after = (
            asyncio.get_running_loop().time() + self.settings.verify_delay_seconds
        )

    async def worker(self, monitor: MonitorState) -> None:
        while True:
            monitor.changed.clear()
            now = asyncio.get_running_loop().time()
            if monitor.blocked:
                await monitor.changed.wait()
                continue
            if now < monitor.retry_at:
                await self.wait_for_change(monitor, monitor.retry_at - now)
                continue
            if monitor.target is None or monitor.maximum is None:
                await self.check(monitor)
            elif monitor.dirty:
                await self.write(monitor)
            elif monitor.check_requested and now >= monitor.check_after:
                await self.check(monitor)
            else:
                timeout = (
                    max(0.0, monitor.check_after - now) if monitor.check_requested else None
                )
                await self.wait_for_change(monitor, timeout)
                continue
            await self.publish(False)

    @staticmethod
    async def wait_for_change(monitor: MonitorState, timeout: float | None) -> None:
        if timeout is None:
            await monitor.changed.wait()
            return
        try:
            await asyncio.wait_for(monitor.changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def close(self) -> None:
        # run() already waited for its workers.
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.popup is not None:
            try:
                await self.popup.close()
            except Exception as error:
                print(f"Optional brightness popup cleanup failed: {error}", flush=True)


async def process_changes(state: CommandState, controller: BrightnessController) -> None:
    loop = asyncio.get_running_loop()
    while True:
        state.changed.clear()
        if state.cycle_requested:
            cycles = state.cycle_requested
            state.cycle_requested = 0
            await controller.cycle_target(cycles)
            continue
        if state.show_requested:
            state.show_requested = False
            await controller.show()
            continue
        if state.toggle_geekmagic_requested:
            requests = state.toggle_geekmagic_requested
            state.toggle_geekmagic_requested = 0
            await controller.toggle_geekmagic(requests)
            continue
        if state.chord_latched:
            delay = max(
                0.0,
                state.last_direction_at + CHORD_RELEASE_SECONDS - loop.time(),
            )
            if delay == 0.0:
                state.chord_latched = False
                continue
            try:
                await asyncio.wait_for(state.changed.wait(), delay)
            except asyncio.TimeoutError:
                state.chord_latched = False
            continue
        if state.pending_directions:
            await asyncio.sleep(CHORD_WINDOW_SECONDS)
            directions = state.pending_directions
            state.pending_directions = set()
            if directions == {-1, 1}:
                state.pending_percent = 0.0
                state.held = None
                state.chord_latched = True
                await controller.cycle_target()
                continue
            delta = state.pending_percent
            state.pending_percent = 0.0
            if delta:
                await controller.apply(delta)
            continue
        delay = state.repeat_delay(loop.time())
        if delay == 0.0:
            direction = state.held
            state.next_repeat = loop.time() + state.settings.repeat_interval_seconds
            if direction is not None:
                await controller.apply(direction * state.settings.step_percent)
            continue
        if delay is None:
            await state.changed.wait()
        else:
            try:
                await asyncio.wait_for(state.changed.wait(), delay)
            except asyncio.TimeoutError:
                pass
