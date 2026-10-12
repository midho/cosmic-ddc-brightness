from __future__ import annotations

import fcntl
import json
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ACTIONS = {
    "up", "down", "press-up", "press-down", "release-up", "release-down",
    "stop", "show", "cycle-monitor", "toggle-geekmagic",
}
WAYLAND_NAME = re.compile(r"^wayland-[A-Za-z0-9_.-]+$")


def runtime_directory() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return base / "cosmic-ddc-brightness"


def prepare_runtime_directory(path: Path | None = None) -> Path:
    selected = path or runtime_directory()
    selected.mkdir(mode=0o700, parents=True, exist_ok=True)
    if selected.is_symlink() or selected.stat().st_uid != os.getuid():
        raise RuntimeError(f"unsafe runtime directory: {selected}")
    os.chmod(selected, 0o700)
    return selected


@dataclass
class InstanceLock:
    descriptor: int

    def close(self) -> None:
        os.close(self.descriptor)


def acquire_instance_lock(directory: Path) -> InstanceLock:
    lock_path = directory / "daemon.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise RuntimeError("another cosmic-ddc-brightness daemon is already running") from error
    return InstanceLock(descriptor)


def session_from_environment(runtime_base: Path | None = None) -> dict[str, str]:
    display = os.environ.get("WAYLAND_DISPLAY", "")
    base = runtime_base or runtime_directory().parent
    if not WAYLAND_NAME.fullmatch(display):
        return {}
    path = base / display
    try:
        if not path.is_socket() or path.stat().st_uid != os.getuid():
            return {}
    except OSError:
        return {}
    return {"wayland_display": display}


def encode_command(action: str, runtime_base: Path | None = None) -> bytes:
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")
    payload: dict[str, Any] = {"action": action}
    payload.update(session_from_environment(runtime_base))
    return json.dumps(payload, separators=(",", ":")).encode()


def decode_command(data: bytes) -> tuple[str, str]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid command message") from error
    if not isinstance(payload, dict) or payload.get("action") not in ACTIONS:
        raise ValueError("invalid command action")
    display = payload.get("wayland_display", "")
    if not isinstance(display, str) or (display and not WAYLAND_NAME.fullmatch(display)):
        display = ""
    return payload["action"], display


def send_command(action: str) -> None:
    socket_path = runtime_directory() / "daemon.sock"
    message = encode_command(action)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    client.settimeout(0.5)
    try:
        client.sendto(message, str(socket_path))
    except OSError as error:
        raise RuntimeError(f"cannot contact the brightness service: {error}") from error
    finally:
        client.close()
