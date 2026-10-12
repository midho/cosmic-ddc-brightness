import asyncio
import dataclasses
import unittest
from unittest.mock import AsyncMock

from cosmic_ddc_brightness.config import (
    GeekMagicConfig,
    MonitorConfig,
    PopupConfig,
    Settings,
)
from cosmic_ddc_brightness.ddc import AmbiguousMonitorError, DdcCommandError, DisconnectedMonitorError
from cosmic_ddc_brightness.controller import (
    BrightnessController,
    CommandState,
    brightness_step,
    device_brightness_step,
    process_changes,
)


def settings() -> Settings:
    return Settings(
        version=1,
        step_percent=5.0,
        hold_delay_seconds=0.25,
        repeat_interval_seconds=0.10,
        hold_limit_seconds=5.0,
        refresh_after_seconds=15.0,
        verify_delay_seconds=0.60,
        ddc_timeout_seconds=3.0,
        scan_before_each_write=True,
        popup=PopupConfig(enabled=False),
        monitors=(MonitorConfig("one", "One", "DEL", "A", "1"),),
    )


class BrightnessMathTests(unittest.TestCase):
    def test_step_is_a_percentage_of_non_100_maximum(self):
        self.assertEqual(brightness_step(127, 254, 5.0), 140)
        self.assertEqual(brightness_step(127, 254, -5.0), 114)

    def test_step_clamps(self):
        self.assertEqual(brightness_step(99, 100, 5.0), 100)
        self.assertEqual(brightness_step(1, 100, -5.0), 0)

    def test_nonzero_subunit_step_moves_in_both_directions(self):
        self.assertEqual(brightness_step(127, 254, 0.1), 128)
        self.assertEqual(brightness_step(127, 254, -0.1), 126)

    def test_subunit_step_moves_inward_from_boundaries(self):
        self.assertEqual(brightness_step(0, 254, 0.1), 1)
        self.assertEqual(brightness_step(254, 254, -0.1), 253)
        self.assertEqual(brightness_step(0, 254, -0.1), 0)
        self.assertEqual(brightness_step(254, 254, 0.1), 254)

    def test_geekmagic_reserves_zero_for_the_power_toggle(self):
        config = GeekMagicConfig(
            "geekmagic", "GeekMagic Ultra", "http://192.168.1.20"
        )
        self.assertEqual(device_brightness_step(config, 0, 100, 5.0), 0)
        self.assertEqual(device_brightness_step(config, 1, 100, -5.0), 1)
        self.assertEqual(device_brightness_step(config, 5, 100, -5.0), 1)
        self.assertEqual(device_brightness_step(config, 1, 100, 5.0), 5)
        self.assertEqual(device_brightness_step(config, 5, 100, 5.0), 10)


class CommandStateTests(unittest.TestCase):
    def test_tap(self):
        state = CommandState(settings())
        state.handle("up", 10.0)
        self.assertEqual(state.pending_percent, 5.0)

    def test_geekmagic_toggle_is_queued(self):
        state = CommandState(settings())
        state.handle("toggle-geekmagic", 10.0)
        self.assertEqual(state.toggle_geekmagic_requested, 1)

    def test_hold_timing_and_release(self):
        state = CommandState(settings())
        state.handle("press-down", 10.0)
        self.assertEqual(state.pending_percent, -5.0)
        self.assertAlmostEqual(state.repeat_delay(10.1), 0.15)
        self.assertEqual(state.repeat_delay(10.25), 0.0)
        state.handle("release-down", 10.3)
        self.assertIsNone(state.repeat_delay(10.3))

    def test_duplicate_press_does_not_extend_hold_limit(self):
        state = CommandState(settings())
        state.handle("press-up", 10.0)
        original_limit = state.hold_until
        state.handle("press-up", 11.0)
        self.assertEqual(state.hold_until, original_limit)

    def test_opposite_release_does_not_stop_new_hold(self):
        state = CommandState(settings())
        state.handle("press-up", 10.0)
        state.handle("press-down", 10.1)
        state.handle("release-up", 10.2)
        self.assertEqual(state.held, -1)


class FakeManager:
    def __init__(self, current=50, maximum=100):
        self.current = current
        self.maximum = maximum
        self.reads = 0
        self.writes = []

    async def read(self, _monitor_id):
        self.reads += 1
        return self.current, self.maximum

    async def write(self, _monitor_id, value):
        self.writes.append(value)
        self.current = value


class ToggleManager(FakeManager):
    def __init__(self, current=50, maximum=100):
        super().__init__(current, maximum)
        self.remembered = 50

    def toggle_target(self, _monitor_id, current):
        if current == 0:
            return self.remembered
        self.remembered = current
        return 0


class SlowManager(FakeManager):
    def __init__(self):
        super().__init__()
        self.first_write_started = asyncio.Event()
        self.release_first_write = asyncio.Event()

    async def write(self, _monitor_id, value):
        self.writes.append(value)
        if len(self.writes) == 1:
            self.first_write_started.set()
            await self.release_first_write.wait()
        self.current = value


class SignalingManager(FakeManager):
    def __init__(self):
        super().__init__()
        self.second_write = asyncio.Event()

    async def write(self, monitor_id, value):
        await super().write(monitor_id, value)
        if len(self.writes) >= 2:
            self.second_write.set()


class PausedReadManager(FakeManager):
    """Pause one read until the test releases it."""

    def __init__(self, current=50, maximum=100, pause_read=2):
        super().__init__(current, maximum)
        self.pause_read = pause_read
        self.read_started = asyncio.Event()
        self.release_read = asyncio.Event()

    async def read(self, _monitor_id):
        self.reads += 1
        if self.reads == self.pause_read:
            self.read_started.set()
            await self.release_read.wait()
        return self.current, self.maximum


class RecoveringWriteManager(FakeManager):
    def __init__(self, current=50, maximum=100):
        super().__init__(current, maximum)
        self.fail_writes = True

    async def write(self, _monitor_id, value):
        if self.fail_writes:
            raise DdcCommandError("write failed")
        await super().write(_monitor_id, value)


class CommandProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_chord_cancels_held_repeat_and_recovers_after_release(self):
        for first, second in (
            ("press-up", "press-down"),
            ("press-down", "press-up"),
        ):
            with self.subTest(order=(first, second)):
                state = CommandState(settings())
                controller = AsyncMock(spec=BrightnessController)
                processor = asyncio.create_task(process_changes(state, controller))
                try:
                    loop = asyncio.get_running_loop()
                    state.handle(first, loop.time())
                    state.handle(second, loop.time())
                    for _ in range(100):
                        if controller.cycle_target.await_count == 1:
                            break
                        await asyncio.sleep(0.005)
                    self.assertEqual(controller.cycle_target.await_count, 1)

                    state.handle("press-up", loop.time())
                    state.handle("press-down", loop.time())
                    await asyncio.sleep(0.75)
                    controller.apply.assert_not_awaited()

                    state.handle("release-up", loop.time())
                    state.handle("release-down", loop.time())
                    state.handle("press-up", loop.time())
                    state.handle("release-up", loop.time())
                    await asyncio.sleep(0.12)
                    controller.apply.assert_awaited_once_with(5.0)
                finally:
                    processor.cancel()
                    await asyncio.gather(processor, return_exceptions=True)

    async def test_opposite_taps_cycle_once_without_changing_brightness(self):
        state = CommandState(settings())
        controller = AsyncMock(spec=BrightnessController)
        processor = asyncio.create_task(process_changes(state, controller))
        try:
            loop = asyncio.get_running_loop()
            state.handle("down", loop.time())
            state.handle("up", loop.time())
            for _ in range(100):
                if controller.cycle_target.await_count == 1:
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(controller.cycle_target.await_count, 1)
            controller.apply.assert_not_awaited()

            # Repeated events from holding both buttons stay inside the latch.
            state.handle("down", loop.time())
            state.handle("up", loop.time())
            await asyncio.sleep(0.15)
            self.assertEqual(controller.cycle_target.await_count, 1)
            controller.apply.assert_not_awaited()
        finally:
            processor.cancel()
            await asyncio.gather(processor, return_exceptions=True)

    async def test_quick_release_during_slow_write_does_not_repeat(self):
        selected = settings()
        manager = SlowManager()
        controller = BrightnessController(selected, manager)
        monitor = next(iter(controller.monitors.values()))
        state = CommandState(selected)
        worker = asyncio.create_task(controller.worker(monitor))
        processor = asyncio.create_task(process_changes(state, controller))
        try:
            for _ in range(100):
                if monitor.target == 50:
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(monitor.target, 50)

            loop = asyncio.get_running_loop()
            state.handle("press-down", loop.time())
            await asyncio.wait_for(manager.first_write_started.wait(), 1.0)
            state.handle("release-down", loop.time())
            manager.release_first_write.set()
            await asyncio.sleep(0.30)

            self.assertEqual(manager.writes, [45])
            self.assertEqual(manager.current, 45)
        finally:
            manager.release_first_write.set()
            processor.cancel()
            worker.cancel()
            await asyncio.gather(processor, worker, return_exceptions=True)

    async def test_hold_keeps_latest_target_during_slow_write(self):
        selected = settings()
        manager = SlowManager()
        controller = BrightnessController(selected, manager)
        monitor = next(iter(controller.monitors.values()))
        state = CommandState(selected)
        worker = asyncio.create_task(controller.worker(monitor))
        processor = asyncio.create_task(process_changes(state, controller))
        try:
            for _ in range(100):
                if monitor.target == 50:
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(monitor.target, 50)

            loop = asyncio.get_running_loop()
            state.handle("press-down", loop.time())
            await asyncio.wait_for(manager.first_write_started.wait(), 1.0)
            await asyncio.sleep(0.35)
            self.assertLess(monitor.target, 45)
            self.assertEqual(manager.writes, [45])
            state.handle("release-down", loop.time())
            target_at_release = monitor.target
            manager.release_first_write.set()
            for _ in range(100):
                if manager.current == target_at_release:
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(manager.writes, [45, target_at_release])
            await asyncio.sleep(0.20)
            self.assertEqual(monitor.target, target_at_release)
            self.assertEqual(manager.writes, [45, target_at_release])
        finally:
            manager.release_first_write.set()
            processor.cancel()
            worker.cancel()
            await asyncio.gather(processor, worker, return_exceptions=True)

    async def test_hold_repeats_after_a_confirmed_write(self):
        selected = dataclasses.replace(settings(), verify_delay_seconds=0.1)
        manager = SignalingManager()
        controller = BrightnessController(selected, manager)
        monitor = next(iter(controller.monitors.values()))
        state = CommandState(selected)
        worker = asyncio.create_task(controller.worker(monitor))
        processor = asyncio.create_task(process_changes(state, controller))
        try:
            for _ in range(100):
                if monitor.target == 50:
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(monitor.target, 50)

            loop = asyncio.get_running_loop()
            state.handle("press-up", loop.time())
            await asyncio.wait_for(manager.second_write.wait(), 1.0)
            state.handle("release-up", loop.time())
            writes_at_release = len(manager.writes)
            await asyncio.sleep(0.30)
            self.assertEqual(len(manager.writes), writes_at_release)
        finally:
            processor.cancel()
            worker.cancel()
            await asyncio.gather(processor, worker, return_exceptions=True)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = settings()
        self.manager = FakeManager()
        self.controller = BrightnessController(self.settings, self.manager)
        monitor = next(iter(self.controller.monitors.values()))
        self.worker = asyncio.create_task(self.controller.worker(monitor))
        for _ in range(20):
            if monitor.target == 50:
                break
            await asyncio.sleep(0)
        self.monitor = monitor

    async def asyncTearDown(self):
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)

    async def wait_for(self, condition):
        for _ in range(100):
            if condition():
                return
            await asyncio.sleep(0.005)
        self.fail("controller did not reach the expected state")

    async def test_fresh_value_changes_without_extra_read(self):
        initial_reads = self.manager.reads
        await self.controller.apply(5.0)
        await self.wait_for(lambda: self.manager.writes == [55])
        self.assertEqual(self.manager.reads, initial_reads)

    async def test_stale_value_is_read_before_change(self):
        self.manager.current = 30
        self.monitor.last_observed_at = -1000.0
        await self.controller.apply(-5.0)
        await self.wait_for(lambda: self.manager.writes == [25])
        self.assertGreaterEqual(self.manager.reads, 2)

    async def test_zero_idle_refresh_uses_last_confirmed_level(self):
        self.controller.settings = dataclasses.replace(
            self.settings, refresh_after_seconds=0.0
        )
        initial_reads = self.manager.reads
        self.manager.current = 30
        self.monitor.last_observed_at = -1000.0
        await self.controller.apply(-5.0)
        await self.wait_for(lambda: self.manager.writes == [45])
        self.assertEqual(self.manager.reads, initial_reads)

    async def test_zero_idle_refresh_still_reads_unknown_level(self):
        self.controller.settings = dataclasses.replace(
            self.settings, refresh_after_seconds=0.0
        )
        self.monitor.target = None
        self.monitor.maximum = None
        await self.controller.apply(-5.0)
        self.assertEqual(self.monitor.pending_percent, -5.0)
        self.assertIsNone(self.monitor.target)

    async def test_stale_request_shows_last_known_percent_during_read(self):
        self.monitor.last_observed_at = -1000.0
        await self.controller.apply(-5.0)
        row = self.controller.snapshot(True)["levels"][0]
        self.assertEqual(row["percent"], self.manager.current)
        self.assertTrue(row["estimated"])
        self.assertEqual(self.manager.current, 50)

    async def test_popup_tracks_completed_writes_during_rapid_requests(self):
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        self.manager = SlowManager()
        self.controller.manager = self.manager
        await self.controller.apply(5.0)
        write = asyncio.create_task(self.controller.write(self.monitor))
        try:
            await self.manager.first_write_started.wait()
            self.assertEqual(self.controller.snapshot(True)["levels"][0]["percent"], self.manager.current)
            await self.controller.apply(5.0)
            self.manager.release_first_write.set()
            await write
            # The completed 55% write is visible even though 60% was requested
            # while that write was running. The next write has not happened.
            self.assertEqual(self.manager.current, 55)
            self.assertEqual(self.controller.snapshot(False)["levels"][0]["percent"], 55)
            await self.controller.write(self.monitor)
            self.assertEqual(self.manager.current, 60)
            self.assertEqual(self.controller.snapshot(False)["levels"][0]["percent"], 60)
            await self.controller.check(self.monitor)
            self.assertFalse(self.controller.snapshot(False)["levels"][0]["estimated"])
        finally:
            self.manager.release_first_write.set()
            await asyncio.gather(write, return_exceptions=True)
    async def test_unplugged_monitor_discards_old_steps_and_recovers_on_reconnect(self):
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        self.monitor.blocked = True
        self.manager.read = AsyncMock(side_effect=DisconnectedMonitorError("not connected"))
        await self.controller.apply(15.0)
        await self.controller.check(self.monitor)
        self.assertTrue(self.monitor.blocked)
        self.assertFalse(self.monitor.active)
        self.assertEqual(self.monitor.pending_percent, 0.0)
        row = self.controller.snapshot(False)["levels"][0]
        self.assertIsNone(row["percent"])
        self.assertEqual(row["status"], "Unavailable")
        self.manager.read = AsyncMock(return_value=(20, 100))
        await self.controller.apply(5.0)
        await self.controller.check(self.monitor)
        self.assertEqual(self.monitor.target, 25)
        await self.controller.write(self.monitor)
        self.assertEqual(self.manager.current, 25)

    async def test_new_request_during_slow_write_is_not_lost(self):
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        self.manager = SlowManager()
        self.controller = BrightnessController(self.settings, self.manager)
        self.monitor = next(iter(self.controller.monitors.values()))
        self.worker = asyncio.create_task(self.controller.worker(self.monitor))
        await self.wait_for(lambda: self.monitor.target == 50)

        await self.controller.apply(5.0)
        await self.manager.first_write_started.wait()
        await self.controller.apply(5.0)
        self.manager.release_first_write.set()
        await self.wait_for(lambda: self.manager.writes == [55, 60])

    async def test_ambiguous_identity_is_reported_without_crashing_worker(self):
        self.manager.write = AsyncMock(side_effect=AmbiguousMonitorError("duplicate identity"))
        await self.controller.apply(5.0)
        await self.wait_for(lambda: self.monitor.failures == 1)
        self.assertFalse(self.worker.done())
        self.assertEqual(self.manager.current, 50)


class GeekMagicControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = dataclasses.replace(
            settings(),
            verify_delay_seconds=0.01,
            geekmagic=(GeekMagicConfig(
                "geekmagic", "GeekMagic Ultra", "http://192.168.1.20"
            ),),
        )
        self.manager = ToggleManager(current=35)
        self.controller = BrightnessController(self.settings, self.manager)
        self.monitor = self.controller.monitors["geekmagic"]
        self.worker = asyncio.create_task(self.controller.worker(self.monitor))
        await self.wait_for(lambda: self.monitor.target == 35)

    async def asyncTearDown(self):
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)

    async def wait_for(self, condition):
        for _ in range(200):
            if condition():
                return
            await asyncio.sleep(0.005)
        self.fail("GeekMagic controller did not reach the expected state")

    async def test_toggle_remembers_and_restores_brightness(self):
        await self.controller.toggle_geekmagic()
        await self.wait_for(lambda: self.manager.writes == [0])
        await self.wait_for(lambda: not self.monitor.active)
        self.assertEqual(self.manager.remembered, 35)

        await self.controller.toggle_geekmagic()
        await self.wait_for(lambda: self.manager.writes == [0, 35])
        await self.wait_for(lambda: not self.monitor.active)
        self.assertEqual(self.monitor.target, 35)

    async def test_cycle_includes_geekmagic_after_ddc_monitor(self):
        self.assertEqual(self.controller.selection_label(), "All displays")
        await self.controller.cycle_target()
        self.assertEqual(self.controller.selection_label(), "One")
        await self.controller.cycle_target()
        self.assertEqual(self.controller.selection_label(), "GeekMagic Ultra")


class PopupCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_popup_failure_does_not_interrupt_controller_cleanup(self):
        popup = AsyncMock()
        popup.close.side_effect = ProcessLookupError("popup already exited")
        controller = BrightnessController(settings(), FakeManager(), popup)
        await controller.close()
        popup.close.assert_awaited_once_with()


class StaleReadTests(unittest.IsolatedAsyncioTestCase):
    """Key presses received during a fresh read."""

    async def asyncSetUp(self):
        self.settings = dataclasses.replace(settings(), verify_delay_seconds=0.1)
        self.worker = None

    async def asyncTearDown(self):
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)

    async def wait_for(self, condition):
        for _ in range(200):
            if condition():
                return
            await asyncio.sleep(0.005)
        self.fail("controller did not reach the expected state")

    async def start(self, manager):
        self.manager = manager
        self.controller = BrightnessController(self.settings, manager)
        self.monitor = next(iter(self.controller.monitors.values()))
        self.worker = asyncio.create_task(self.controller.worker(self.monitor))

    async def start_with_stale_cache(self):
        # Cache 50, then pause the stale-value refresh.
        await self.start(PausedReadManager(current=50, pause_read=2))
        await self.wait_for(lambda: self.monitor.target == 50)
        self.assertFalse(self.monitor.active)
        # Physical buttons changed the monitor after the cached read.
        self.manager.current = 30
        self.monitor.last_observed_at = -1000.0

    async def assert_settled(self, writes, target):
        await self.wait_for(lambda: self.manager.writes == writes)
        await self.wait_for(lambda: not self.monitor.active)
        self.assertEqual(self.manager.writes, writes)
        self.assertEqual(self.monitor.target, target)
        self.assertEqual(self.monitor.observed, target)
        self.assertEqual(self.monitor.pending_percent, 0.0)
        self.assertFalse(self.monitor.dirty)
        self.assertFalse(self.monitor.check_requested)
        self.assertEqual(self.monitor.status, "")

    async def test_repeated_requests_during_stale_read_apply_once_to_fresh_value(self):
        await self.start_with_stale_cache()

        await self.controller.apply(-5.0)
        await self.manager.read_started.wait()
        self.assertIsNone(self.monitor.target)
        self.assertEqual(self.monitor.pending_percent, -5.0)

        for _ in range(3):
            await self.controller.apply(-5.0)
        # The cached 50 cannot be the next step's starting point.
        self.assertIsNone(self.monitor.target)
        self.assertEqual(self.monitor.pending_percent, -20.0)
        self.assertFalse(self.monitor.dirty)
        self.assertTrue(self.monitor.check_requested)
        self.assertEqual(self.manager.writes, [])

        self.manager.release_read.set()
        await self.assert_settled(writes=[10], target=10)
        self.assertEqual(self.manager.reads, 3)

    async def test_new_target_during_verification_read_skips_stale_result(self):
        await self.start(PausedReadManager(current=50, pause_read=2))
        await self.wait_for(lambda: self.monitor.target == 50)
        await self.controller.apply(5.0)
        await self.manager.read_started.wait()
        await self.controller.apply(5.0)
        self.manager.release_read.set()
        await self.wait_for(lambda: self.manager.writes == [55, 60])
        self.assertEqual(self.monitor.failures, 0)
        await self.assert_settled(writes=[55, 60], target=60)

    async def test_opposite_requests_during_stale_read_cancel_without_write(self):
        await self.start_with_stale_cache()
        await self.controller.apply(5.0)
        await self.manager.read_started.wait()
        await self.controller.apply(-5.0)
        self.assertEqual(self.monitor.pending_percent, 0.0)
        self.manager.release_read.set()
        await self.wait_for(lambda: self.monitor.target == 30)
        await self.assert_settled(writes=[], target=30)

    async def test_requests_during_initial_read_apply_once(self):
        # Pause the first read, before anything is cached.
        await self.start(PausedReadManager(current=30, pause_read=1))
        await self.manager.read_started.wait()
        self.assertIsNone(self.monitor.maximum)
        await self.controller.apply(5.0)
        await self.controller.apply(5.0)
        self.assertIsNone(self.monitor.target)
        self.assertEqual(self.monitor.pending_percent, 10.0)
        self.manager.release_read.set()
        await self.assert_settled(writes=[40], target=40)

    async def test_blocked_monitor_reads_again_before_new_change(self):
        manager = RecoveringWriteManager()
        await self.start(manager)
        await self.wait_for(lambda: self.monitor.target == 50)
        # Trigger four failed writes without worker scheduling noise.
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        await self.controller.apply(5.0)
        for _ in range(4):
            await self.controller.write(self.monitor)
        self.assertTrue(self.monitor.blocked)
        self.assertEqual(self.monitor.target, 55)
        self.assertEqual(self.monitor.observed, 50)

        # Physical controls changed the monitor. The cached read is still
        # fresh, leaving the blocked state as the reason to reread.
        manager.current = 20
        manager.fail_writes = False
        reads_before = manager.reads
        self.worker = asyncio.create_task(self.controller.worker(self.monitor))

        await self.controller.apply(5.0)
        self.assertIsNone(self.monitor.target)
        self.assertEqual(self.monitor.pending_percent, 5.0)
        await self.assert_settled(writes=[25], target=25)
        self.assertGreater(manager.reads, reads_before)


class SlowCleanupManager(FakeManager):
    """Reads block until cancelled, then take a while to clean up.

    The read of the ``fail`` monitor instead raises an unexpected error once
    ``fail_now`` is set.
    """

    def __init__(self, cleanup_seconds, fail=None):
        super().__init__()
        self.cleanup_seconds = cleanup_seconds
        self.fail = fail
        self.fail_now = asyncio.Event()
        self.cancels = {}
        self.cleaned_up = set()

    async def read(self, monitor_id):
        self.reads += 1
        try:
            if monitor_id == self.fail:
                await self.fail_now.wait()
                raise RuntimeError("unexpected failure")
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancels[monitor_id] = self.cancels.get(monitor_id, 0) + 1
            await asyncio.sleep(self.cleanup_seconds[monitor_id])
            self.cleaned_up.add(monitor_id)
            raise
        return self.current, self.maximum


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, manager):
        two_monitors = dataclasses.replace(
            settings(),
            monitors=(
                MonitorConfig("one", "One", "DEL", "A", "1"),
                MonitorConfig("two", "Two", "DEL", "B", "2"),
            ),
        )
        self.manager = manager
        self.controller = BrightnessController(two_monitors, manager)
        self.run_task = asyncio.create_task(self.controller.run())
        for _ in range(200):
            if manager.reads == 2:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(manager.reads, 2)

    async def asyncTearDown(self):
        await self.controller.close()
        await asyncio.gather(self.run_task, return_exceptions=True)

    async def test_cancelled_run_waits_for_every_worker_cleanup(self):
        await self.start(SlowCleanupManager({"one": 0.01, "two": 0.2}))
        # Shutdown cancels run() once and waits for its workers.
        self.run_task.cancel()
        await asyncio.gather(self.run_task, return_exceptions=True)
        self.assertTrue(self.run_task.cancelled())
        # Each worker finished cleanup before run() returned.
        self.assertTrue(all(task.done() for task in self.controller.tasks))
        self.assertEqual(self.manager.cancels, {"one": 1, "two": 1})
        self.assertEqual(self.manager.cleaned_up, {"one", "two"})
        await self.controller.close()
        self.assertEqual(self.manager.cancels, {"one": 1, "two": 1})

    async def test_failed_worker_stops_its_siblings(self):
        await self.start(SlowCleanupManager({"one": 0.01, "two": 0.05}, fail="one"))
        self.manager.fail_now.set()
        with self.assertRaisesRegex(RuntimeError, "unexpected failure"):
            await self.run_task
        self.assertTrue(all(task.done() for task in self.controller.tasks))
        self.assertEqual(self.manager.cancels, {"two": 1})
        self.assertEqual(self.manager.cleaned_up, {"two"})


if __name__ == "__main__":
    unittest.main()
