from __future__ import annotations

import argparse
import asyncio
import os
import signal
import shutil
import socket
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, config_path, load_config, render_config, write_config
from .controller import BrightnessController, CommandState, process_changes
from .devices import DeviceManager
from .ddc import (
    AmbiguousMonitorError,
    DdcClient,
    DdcCommandError,
    DdcManager,
    configurations_from_detected,
    match_monitors,
)
from .popup import BrightnessPopup
from .errors import DeviceCommandError
from .geekmagic import GeekMagicClient
from .runtime import (
    ACTIONS,
    acquire_instance_lock,
    decode_command,
    prepare_runtime_directory,
    send_command,
)


async def configure(selected_path: Path, force: bool) -> int:
    client = DdcClient()
    try:
        detected = await client.detect()
        if not detected:
            print("No DDC/CI monitors were detected.", file=sys.stderr)
            return 1
        monitors = configurations_from_detected(detected)
        write_config(selected_path, render_config(monitors), force=force)
    except (DdcCommandError, AmbiguousMonitorError, ConfigError) as error:
        print(f"Configuration failed: {error}", file=sys.stderr)
        return 1
    print(f"Wrote {selected_path}")
    for monitor, found in zip(monitors, detected):
        serial = monitor.serial or "no EDID serial"
        print(f"  {monitor.name}: {monitor.manufacturer}:{monitor.model}:{serial} (current bus {found.bus})")
    return 0


async def doctor(selected_path: Path) -> int:
    failures = 0
    warnings = 0

    def report(kind: str, message: str) -> None:
        print(f"{kind}: {message}")

    executable = shutil.which("ddcutil")
    if executable is None:
        report("FAIL", "ddcutil is not installed")
        return 1

    client = DdcClient(executable=executable)
    try:
        report("PASS", await client.version())
    except DdcCommandError as error:
        report("FAIL", str(error))
        return 1

    try:
        settings = load_config(selected_path)
        report("PASS", f"configuration is valid: {selected_path}")
    except ConfigError as error:
        report("FAIL", str(error))
        return 1
    if not settings.scan_before_each_write:
        report("WARN", "fast writes do not rescan for duplicate monitor identities before each change")
        warnings += 1

    try:
        detected = await client.detect()
        report("PASS", f"detected {len(detected)} DDC/CI monitor(s)")
        matched = match_monitors(settings.monitors, detected)
    except (DdcCommandError, AmbiguousMonitorError) as error:
        report("FAIL", str(error))
        return 1

    connectors: dict[int, str] = {}
    if not settings.scan_before_each_write:
        try:
            connectors = await client.detect_connectors()
        except DdcCommandError as error:
            report("WARN", f"cannot check fast bus support: {error}")
            warnings += 1

    for monitor in settings.monitors:
        found = matched.get(monitor.id)
        if found is None:
            report("FAIL", f"{monitor.name} is not connected or does not match its EDID identity")
            failures += 1
            continue
        if not settings.scan_before_each_write:
            connector = connectors.get(found.bus, "")
            if client.prepare_bus_guard(found, connector) is None:
                report("WARN", f"{monitor.name}: fast guarded writes are unavailable. EDID-selected writes will be used")
                warnings += 1
            else:
                report("PASS", f"{monitor.name}: fast guarded bus writes are available")
        try:
            current, maximum = await client.read_brightness(found)
            report(
                "PASS",
                f"{monitor.name} responds on current bus {found.bus}: {round(100 * current / maximum)}% ({current}/{maximum})",
            )
        except DdcCommandError as error:
            report("FAIL", f"{monitor.name}: {error}")
            failures += 1

    for device in settings.geekmagic:
        client = GeekMagicClient(device)
        try:
            current, maximum = await client.read_brightness()
            report(
                "PASS",
                f"{device.name} responds at {device.url}: "
                f"{round(100 * current / maximum)}% ({current}/{maximum})",
            )
        except DeviceCommandError as error:
            report("FAIL", f"{device.name}: {error}")
            failures += 1

    if settings.popup.enabled:
        try:
            import gi
            gi.require_version("Gtk", "3.0")
            gi.require_version("GtkLayerShell", "0.1")
            from gi.repository import Gtk, GtkLayerShell
            _ = Gtk, GtkLayerShell
        except (ImportError, ValueError):
            report(
                "WARN",
                "GTK 3 or GTK Layer Shell bindings are missing. Brightness control works without the popup",
            )
            warnings += 1
        else:
            report("PASS", "GTK 3 and GTK Layer Shell bindings are installed")

    display = os.environ.get("WAYLAND_DISPLAY", "")
    if display:
        report("PASS", f"Wayland session is visible as {display}")
    else:
        report("WARN", "this shell has no WAYLAND_DISPLAY. A desktop shortcut can still supply it")
        warnings += 1

    if failures:
        print(f"Result: {failures} failure(s), {warnings} warning(s)")
        return 1
    print(f"Result: ready with {warnings} warning(s)")
    return 0


async def receive_commands(
    server: socket.socket,
    state: CommandState,
    popup: BrightnessPopup | None,
) -> None:
    loop = asyncio.get_running_loop()
    while True:
        data = await loop.sock_recv(server, 4096)
        try:
            action, display = decode_command(data)
        except ValueError as error:
            print(f"Ignored invalid local command: {error}", flush=True)
            continue
        if popup is not None and display:
            popup.set_wayland_display(display)
        state.handle(action, loop.time())


async def daemon(selected_path: Path) -> int:
    if os.geteuid() == 0:
        print("Do not run this user service as root.", file=sys.stderr)
        return 1
    try:
        settings = load_config(selected_path)
        directory = prepare_runtime_directory()
        instance_lock = acquire_instance_lock(directory)
    except (ConfigError, RuntimeError, OSError) as error:
        print(error, file=sys.stderr)
        return 1

    socket_path = directory / "daemon.sock"
    socket_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.setblocking(False)
    try:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
    except OSError as error:
        server.close()
        instance_lock.close()
        print(f"cannot create the local command socket: {error}", file=sys.stderr)
        return 1

    client = DdcClient(timeout=settings.ddc_timeout_seconds)
    ddc_manager = DdcManager(
        settings.monitors,
        client,
        scan_before_each_write=settings.scan_before_each_write,
    )
    try:
        await ddc_manager.refresh()
    except AmbiguousMonitorError as error:
        print(error, file=sys.stderr)
        server.close()
        socket_path.unlink(missing_ok=True)
        instance_lock.close()
        return 1
    except DdcCommandError as error:
        print(f"Initial monitor detection failed. Commands will retry: {error}", flush=True)

    manager = DeviceManager(
        ddc_manager,
        {
            config.id: GeekMagicClient(config)
            for config in settings.geekmagic
        },
    )
    popup = BrightnessPopup(settings.popup) if settings.popup.enabled else None
    controller = BrightnessController(settings, manager, popup)
    state = CommandState(settings)
    loop = asyncio.get_running_loop()
    stop_requested = asyncio.Event()
    handled_signals = (signal.SIGINT, signal.SIGTERM)
    for selected_signal in handled_signals:
        loop.add_signal_handler(selected_signal, stop_requested.set)
    print("COSMIC DDC brightness controller is ready.", flush=True)
    tasks = [
        asyncio.create_task(receive_commands(server, state, popup)),
        asyncio.create_task(process_changes(state, controller)),
        asyncio.create_task(controller.run()),
    ]
    stop_task = asyncio.create_task(stop_requested.wait())
    try:
        done, _ = await asyncio.wait(
            [*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
        )
        for finished in done:
            if finished is not stop_task:
                finished.result()
    finally:
        for task in tasks:
            task.cancel()
        stop_task.cancel()
        await asyncio.gather(*tasks, stop_task, return_exceptions=True)
        await controller.close()
        server.close()
        socket_path.unlink(missing_ok=True)
        instance_lock.close()
        for selected_signal in handled_signals:
            loop.remove_signal_handler(selected_signal)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cosmic-ddc-brightness",
        description="External display brightness controls for COSMIC",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=config_path())
    subparsers = parser.add_subparsers(dest="command", required=True)
    configure_parser = subparsers.add_parser("configure", help="detect monitors and write a configuration")
    configure_parser.add_argument("--force", action="store_true", help="replace an existing configuration")
    subparsers.add_parser("doctor", help="run read-only checks")
    subparsers.add_parser("daemon", help="run the background controller")
    for action in sorted(ACTIONS):
        subparsers.add_parser(action)
    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    try:
        if arguments.command == "configure":
            status = asyncio.run(configure(arguments.config, arguments.force))
        elif arguments.command == "doctor":
            status = asyncio.run(doctor(arguments.config))
        elif arguments.command == "daemon":
            status = asyncio.run(daemon(arguments.config))
        else:
            send_command(arguments.command)
            status = 0
    except (KeyboardInterrupt, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        status = 1
    raise SystemExit(status)
