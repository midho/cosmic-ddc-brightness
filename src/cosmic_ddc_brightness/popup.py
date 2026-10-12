from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from .config import PopupConfig
from .runtime import WAYLAND_NAME, runtime_directory


MAX_POPUP_MESSAGE_BYTES = 65_536
POPUP_WRITE_TIMEOUT_SECONDS = 0.1
POPUP_CLOSE_GRACE_SECONDS = 1.0
POPUP_STOP_TIMEOUT_SECONDS = 0.5


def display_value(row: dict) -> tuple[str, float]:
    percent = row.get("percent")
    if percent is None:
        return str(row.get("status") or "Unavailable"), 0.0
    percent = max(0, min(100, int(percent)))
    prefix = "≈" if row.get("estimated") else ""
    label = f"{prefix}{percent}%"
    if row.get("status") == "Not confirmed":
        label += " (not confirmed)"
    return label, percent / 100.0


class BrightnessPopup:
    def __init__(self, config: PopupConfig):
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self.wayland_display = ""
        self.last_failure = float("-inf")

    def set_wayland_display(self, display: str) -> None:
        if display and WAYLAND_NAME.fullmatch(display):
            self.wayland_display = display

    def environment(self) -> dict[str, str] | None:
        display = self.wayland_display or os.environ.get("WAYLAND_DISPLAY", "")
        runtime_base = runtime_directory().parent
        if not WAYLAND_NAME.fullmatch(display):
            return None
        display_path = runtime_base / display
        try:
            if not display_path.is_socket() or display_path.stat().st_uid != os.getuid():
                return None
        except OSError:
            return None
        return {
            **os.environ,
            "GDK_BACKEND": "wayland",
            "XDG_RUNTIME_DIR": str(runtime_base),
            "WAYLAND_DISPLAY": display,
            "XDG_SESSION_TYPE": "wayland",
        }

    async def publish(self, message: dict) -> None:
        data = json.dumps(message).encode() + b"\n"
        if len(data) > MAX_POPUP_MESSAGE_BYTES:
            print("Brightness popup update is too large. Update dropped.", flush=True)
            return
        if self.process is None or self.process.returncode is not None:
            if not message.get("show"):
                return
            now = asyncio.get_running_loop().time()
            if now - self.last_failure < 10.0:
                return
            environment = self.environment()
            if environment is None:
                self.last_failure = now
                print(
                    "Brightness popup unavailable until a command supplies a valid Wayland display.",
                    flush=True,
                )
                return
            arguments = [
                sys.executable, "-m", "cosmic_ddc_brightness.popup",
                "--duration-ms", str(self.config.duration_ms),
                "--bottom-margin", str(self.config.bottom_margin),
            ]
            if self.config.monitor_model:
                arguments.extend(["--monitor-model", self.config.monitor_model])
            if self.config.all_monitors:
                arguments.append("--all-monitors")
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *arguments,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    env=environment,
                )
            except OSError as error:
                self.last_failure = now
                print(f"Brightness popup unavailable: {error}", flush=True)
                return
        try:
            if self.process.stdin is not None:
                self.process.stdin.write(data)
                await asyncio.wait_for(
                    self.process.stdin.drain(), POPUP_WRITE_TIMEOUT_SECONDS
                )
        except asyncio.TimeoutError:
            # A non-reading popup is stuck. Pause restarts for ten seconds.
            print("Brightness popup stopped reading. Update dropped.", flush=True)
            await self._discard_process(pause_restarts=True)
        except (BrokenPipeError, ConnectionError, RuntimeError) as error:
            # A closed popup can restart on the next action.
            print(f"Brightness popup disconnected: {error}", flush=True)
            await self._discard_process(pause_restarts=False)

    async def _discard_process(self, pause_restarts: bool) -> None:
        process = self.process
        self.process = None
        if pause_restarts:
            self.last_failure = asyncio.get_running_loop().time()
        if process is None:
            return
        self._close_stdin(process)
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await self._wait_for_exit(process, POPUP_STOP_TIMEOUT_SECONDS)

    @staticmethod
    def _close_stdin(process: asyncio.subprocess.Process) -> None:
        if process.stdin is None:
            return
        try:
            process.stdin.close()
        except (BrokenPipeError, ConnectionError, RuntimeError):
            pass

    @staticmethod
    async def _wait_for_exit(
        process: asyncio.subprocess.Process, timeout: float
    ) -> bool:
        if process.returncode is not None:
            return True
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except asyncio.TimeoutError:
            return process.returncode is not None
        return True

    async def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        self._close_stdin(process)
        if await self._wait_for_exit(process, POPUP_CLOSE_GRACE_SECONDS):
            return
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        # A child watcher can lag even after the process exits. Never hold up
        # service shutdown while waiting for it.
        await self._wait_for_exit(process, POPUP_STOP_TIMEOUT_SECONDS)


def run_popup(
    duration_ms: int,
    bottom_margin: int,
    monitor_model: str,
    all_monitors: bool,
) -> int:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import Gdk, GLib, Gtk, GtkLayerShell

    ready, _ = Gtk.init_check([])
    if not ready or not GtkLayerShell.is_supported():
        print("Brightness popup: the Wayland desktop is not available.", file=sys.stderr)
        return 1

    display = Gdk.Display.get_default()
    monitors = [display.get_monitor(index) for index in range(display.get_n_monitors())]
    if all_monitors:
        popup_monitors = monitors or [None]
    else:
        popup_monitor = None
        for index in range(display.get_n_monitors()):
            candidate = display.get_monitor(index)
            if monitor_model and (
                candidate.get_model() or ""
            ).casefold() == monitor_model.casefold():
                popup_monitor = candidate
                break
        popup_monitors = [popup_monitor]

    css = Gtk.CssProvider()
    css.load_from_data(b"""
        #brightness-popup { background-color: transparent; }
        #brightness-card {
            background-color: rgba(30, 29, 36, 0.97);
            border: 1px solid rgba(255, 255, 255, 0.16);
            border-radius: 18px;
            padding: 18px 22px 22px;
        }
        #brightness-card label { color: #f5f3fa; font-size: 14px; }
        #brightness-card image { color: #ded9ee; }
        #brightness-title { font-weight: bold; font-size: 15px; }
        #brightness-card .percentage { color: #d8ccff; }
        #brightness-card progressbar trough {
            min-height: 7px; border: none; border-radius: 8px;
            background-image: none; background-color: #494550;
        }
        #brightness-card progressbar progress {
            min-height: 7px; border: none; border-radius: 8px;
            background-image: none; background-color: #b8a0f6;
        }
    """)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

    views = []
    for popup_monitor in popup_monitors:
        window = Gtk.Window(title="Display brightness")
        window.set_name("brightness-popup")
        window.set_decorated(False)
        window.set_resizable(False)
        window.set_accept_focus(False)
        window.set_skip_taskbar_hint(True)
        window.set_skip_pager_hint(True)
        window.set_app_paintable(True)
        visual = window.get_screen().get_rgba_visual()
        if visual:
            window.set_visual(visual)

        GtkLayerShell.init_for_window(window)
        if popup_monitor is not None:
            GtkLayerShell.set_monitor(window, popup_monitor)
        GtkLayerShell.set_namespace(window, "cosmic-ddc-brightness-popup")
        GtkLayerShell.set_layer(window, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_keyboard_mode(window, GtkLayerShell.KeyboardMode.NONE)
        GtkLayerShell.set_exclusive_zone(window, 0)
        GtkLayerShell.set_anchor(window, GtkLayerShell.Edge.BOTTOM, True)
        GtkLayerShell.set_margin(window, GtkLayerShell.Edge.BOTTOM, bottom_margin)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        card.set_name("brightness-card")
        card.set_size_request(310, -1)
        window.add(card)
        heading = Gtk.Box(spacing=9)
        heading.pack_start(
            Gtk.Image.new_from_icon_name(
                "display-brightness-symbolic", Gtk.IconSize.BUTTON
            ),
            False,
            False,
            0,
        )
        title = Gtk.Label(label="Brightness", xalign=0)
        title.set_name("brightness-title")
        heading.pack_start(title, False, False, 0)
        card.pack_start(heading, False, False, 0)
        views.append((window, title, card, []))

    def ensure_rows(card, rows: list[tuple], count: int) -> None:
        while len(rows) < count:
            container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
            labels = Gtk.Box(spacing=16)
            name = Gtk.Label(xalign=0)
            value = Gtk.Label(xalign=1)
            value.get_style_context().add_class("percentage")
            labels.pack_start(name, True, True, 0)
            labels.pack_end(value, False, False, 0)
            bar = Gtk.ProgressBar()
            container.pack_start(labels, False, False, 0)
            container.pack_start(bar, False, False, 0)
            card.pack_start(container, False, False, 0)
            rows.append((container, name, value, bar))

    timer = None

    def hide() -> bool:
        nonlocal timer
        timer = None
        # Exit now. The next popup gets a fresh Wayland surface.
        Gtk.main_quit()
        return GLib.SOURCE_REMOVE

    def update(message: dict) -> None:
        nonlocal timer
        levels = message.get("levels", [])
        if not isinstance(levels, list):
            return
        for window, title, card, rows in views:
            ensure_rows(card, rows, len(levels))
            title.set_text(str(message.get("title", "Brightness")))
            for index, widgets in enumerate(rows):
                container, name, value, bar = widgets
                if index >= len(levels):
                    container.hide()
                    continue
                row = levels[index]
                container.show_all()
                container.set_opacity(1.0 if row.get("selected", True) else 0.42)
                name.set_text(str(row.get("name", "Monitor")))
                label, fraction = display_value(row)
                value.set_text(label)
                bar.set_fraction(fraction)
        if message.get("show"):
            for window, _title, _card, rows in views:
                window.show_all()
                for index in range(len(levels), len(rows)):
                    rows[index][0].hide()
            if timer is not None:
                GLib.source_remove(timer)
            timer = GLib.timeout_add(duration_ms, hide)

    buffer = bytearray()
    descriptor = sys.stdin.fileno()
    os.set_blocking(descriptor, False)

    def receive(_source, condition):
        try:
            data = os.read(descriptor, 65536)
        except BlockingIOError:
            return GLib.SOURCE_CONTINUE
        if not data:
            Gtk.main_quit()
            return GLib.SOURCE_REMOVE
        buffer.extend(data)
        while b"\n" in buffer:
            line, _, remaining = buffer.partition(b"\n")
            buffer[:] = remaining
            try:
                update(json.loads(line))
            except (ValueError, TypeError):
                continue
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            Gtk.main_quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    GLib.io_add_watch(descriptor, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, receive)
    for window, _title, _card, _rows in views:
        window.connect("destroy", Gtk.main_quit)
    Gtk.main()
    return 0


def popup_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-ms", type=int, required=True)
    parser.add_argument("--bottom-margin", type=int, required=True)
    parser.add_argument("--monitor-model", default="")
    parser.add_argument("--all-monitors", action="store_true")
    arguments = parser.parse_args()
    try:
        return run_popup(
            arguments.duration_ms,
            arguments.bottom_margin,
            arguments.monitor_model,
            arguments.all_monitors,
        )
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(popup_main())
