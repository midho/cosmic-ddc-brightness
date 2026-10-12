from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class MonitorConfig:
    id: str
    name: str
    manufacturer: str
    model: str
    serial: str


@dataclass(frozen=True)
class GeekMagicConfig:
    id: str
    name: str
    url: str
    timeout_seconds: float = 1.5
    restore_brightness: int = 50


DeviceConfig = MonitorConfig | GeekMagicConfig


@dataclass(frozen=True)
class PopupConfig:
    enabled: bool = True
    duration_ms: int = 1800
    bottom_margin: int = 100
    all_monitors: bool = False
    monitor_model: str = ""


@dataclass(frozen=True)
class Settings:
    version: int
    step_percent: float
    hold_delay_seconds: float
    repeat_interval_seconds: float
    hold_limit_seconds: float
    refresh_after_seconds: float
    verify_delay_seconds: float
    ddc_timeout_seconds: float
    scan_before_each_write: bool
    popup: PopupConfig
    monitors: tuple[MonitorConfig, ...]
    geekmagic: tuple[GeekMagicConfig, ...] = ()

    @property
    def devices(self) -> tuple[DeviceConfig, ...]:
        return (*self.monitors, *self.geekmagic)


DEFAULTS: dict[str, Any] = {
    "version": 1,
    "step_percent": 5.0,
    "hold_delay_seconds": 0.25,
    "repeat_interval_seconds": 0.10,
    "hold_limit_seconds": 5.0,
    "refresh_after_seconds": 15.0,
    "verify_delay_seconds": 0.60,
    "ddc_timeout_seconds": 3.0,
    "scan_before_each_write": True,
}


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "cosmic-ddc-brightness" / "config.toml"


def _number(data: dict[str, Any], name: str, minimum: float, maximum: float) -> float:
    value = data.get(name, DEFAULTS[name])
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return result


def _integer(data: dict[str, Any], name: str, default: int, minimum: int, maximum: int) -> int:
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"popup.{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"popup.{name} must be between {minimum} and {maximum}")
    return value


def _text(data: dict[str, Any], name: str, *, required: bool = True) -> str:
    value = data.get(name, "")
    if not isinstance(value, str):
        raise ConfigError(f"monitor {name} must be text")
    value = value.strip()
    if required and not value:
        raise ConfigError(f"monitor {name} cannot be empty")
    return value


def _device_id(raw: dict[str, Any], ids: set[str]) -> str:
    device_id = _text(raw, "id").lower()
    if not ID_PATTERN.fullmatch(device_id):
        raise ConfigError(
            f"device id {device_id!r} must use lowercase letters, numbers, _ or -"
        )
    if device_id in ids:
        raise ConfigError(f"duplicate device id: {device_id}")
    ids.add(device_id)
    return device_id


def _geekmagic_url(raw: dict[str, Any]) -> str:
    value = _text(raw, "url")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as error:
        raise ConfigError(f"invalid GeekMagic url: {value}") from error
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("GeekMagic url must be an HTTP device address without a path")
    try:
        _ = parsed.port
    except ValueError as error:
        raise ConfigError(f"invalid GeekMagic url: {value}") from error
    return value.rstrip("/")


def parse_config(data: dict[str, Any]) -> Settings:
    version = data.get("version", 1)
    if version != 1:
        raise ConfigError(f"unsupported configuration version: {version!r}")
    scan_before_each_write = data.get("scan_before_each_write", True)
    if not isinstance(scan_before_each_write, bool):
        raise ConfigError("scan_before_each_write must be true or false")

    popup_data = data.get("popup", {})
    if not isinstance(popup_data, dict):
        raise ConfigError("popup must be a table")
    enabled = popup_data.get("enabled", True)
    all_monitors = popup_data.get("all_monitors", False)
    monitor_model = popup_data.get("monitor_model", "")
    if not isinstance(enabled, bool):
        raise ConfigError("popup.enabled must be true or false")
    if not isinstance(all_monitors, bool):
        raise ConfigError("popup.all_monitors must be true or false")
    if not isinstance(monitor_model, str):
        raise ConfigError("popup.monitor_model must be text")
    popup = PopupConfig(
        enabled=enabled,
        duration_ms=_integer(popup_data, "duration_ms", 1800, 250, 10000),
        bottom_margin=_integer(popup_data, "bottom_margin", 100, 0, 1000),
        all_monitors=all_monitors,
        monitor_model=monitor_model.strip(),
    )

    raw_monitors = data.get("monitors")
    if not isinstance(raw_monitors, list) or not raw_monitors:
        raise ConfigError("at least one [[monitors]] entry is required")

    monitors: list[MonitorConfig] = []
    ids: set[str] = set()
    identities: set[tuple[str, str, str]] = set()
    for raw_monitor in raw_monitors:
        if not isinstance(raw_monitor, dict):
            raise ConfigError("each monitors entry must be a table")
        monitor_id = _device_id(raw_monitor, ids)
        monitor = MonitorConfig(
            id=monitor_id,
            name=_text(raw_monitor, "name"),
            manufacturer=_text(raw_monitor, "manufacturer").upper(),
            model=_text(raw_monitor, "model"),
            serial=_text(raw_monitor, "serial", required=False),
        )
        identity = (
            monitor.manufacturer.casefold(),
            monitor.model.casefold(),
            monitor.serial.casefold(),
        )
        if identity in identities:
            raise ConfigError(
                f"monitors must not have the same identity: {monitor.manufacturer}:{monitor.model}:{monitor.serial}"
            )
        identities.add(identity)
        monitors.append(monitor)

    raw_geekmagic = data.get("geekmagic", [])
    if not isinstance(raw_geekmagic, list):
        raise ConfigError("geekmagic must be an array of tables")
    if len(raw_geekmagic) > 1:
        raise ConfigError("only one GeekMagic display is currently supported")
    geekmagic: list[GeekMagicConfig] = []
    for raw_device in raw_geekmagic:
        if not isinstance(raw_device, dict):
            raise ConfigError("each geekmagic entry must be a table")
        timeout = raw_device.get("timeout_seconds", 1.5)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ConfigError("GeekMagic timeout_seconds must be a number")
        timeout = float(timeout)
        if not 0.2 <= timeout <= 10.0:
            raise ConfigError("GeekMagic timeout_seconds must be between 0.2 and 10.0")
        restore = raw_device.get("restore_brightness", 50)
        if isinstance(restore, bool) or not isinstance(restore, int):
            raise ConfigError("GeekMagic restore_brightness must be an integer")
        if not 1 <= restore <= 100:
            raise ConfigError("GeekMagic restore_brightness must be between 1 and 100")
        geekmagic.append(GeekMagicConfig(
            id=_device_id(raw_device, ids),
            name=_text(raw_device, "name"),
            url=_geekmagic_url(raw_device),
            timeout_seconds=timeout,
            restore_brightness=restore,
        ))

    return Settings(
        version=1,
        step_percent=_number(data, "step_percent", 0.1, 25.0),
        hold_delay_seconds=_number(data, "hold_delay_seconds", 0.05, 5.0),
        repeat_interval_seconds=_number(data, "repeat_interval_seconds", 0.05, 2.0),
        hold_limit_seconds=_number(data, "hold_limit_seconds", 0.5, 30.0),
        refresh_after_seconds=_number(data, "refresh_after_seconds", 0.0, 3600.0),
        verify_delay_seconds=_number(data, "verify_delay_seconds", 0.1, 10.0),
        ddc_timeout_seconds=_number(data, "ddc_timeout_seconds", 0.5, 15.0),
        scan_before_each_write=scan_before_each_write,
        popup=popup,
        monitors=tuple(monitors),
        geekmagic=tuple(geekmagic),
    )


def load_config(path: Path | None = None) -> Settings:
    selected = path or config_path()
    try:
        with selected.open("rb") as config_file:
            return parse_config(tomllib.load(config_file))
    except FileNotFoundError as error:
        raise ConfigError(f"configuration not found: {selected}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML in {selected}: {error}") from error
    except UnicodeDecodeError as error:
        raise ConfigError(f"configuration is not valid UTF-8: {selected}") from error
    except OSError as error:
        raise ConfigError(f"cannot read configuration {selected}: {error}") from error


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_config(monitors: list[MonitorConfig]) -> str:
    lines = [
        "version = 1",
        "step_percent = 5.0",
        "hold_delay_seconds = 0.25",
        "repeat_interval_seconds = 0.10",
        "hold_limit_seconds = 5.0",
        "refresh_after_seconds = 15.0",
        "verify_delay_seconds = 0.60",
        "ddc_timeout_seconds = 3.0",
        "scan_before_each_write = true",
        "",
        "[popup]",
        "enabled = true",
        "duration_ms = 1800",
        "bottom_margin = 100",
        "all_monitors = false",
        "monitor_model = \"\"",
    ]
    for monitor in monitors:
        lines.extend([
            "",
            "[[monitors]]",
            f"id = {_toml_string(monitor.id)}",
            f"name = {_toml_string(monitor.name)}",
            f"manufacturer = {_toml_string(monitor.manufacturer)}",
            f"model = {_toml_string(monitor.model)}",
            f"serial = {_toml_string(monitor.serial)}",
        ])
    return "\n".join(lines) + "\n"


def write_config(path: Path, content: str, *, force: bool = False) -> None:
    temporary_name = ""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise ConfigError(f"refusing to replace a symbolic link: {path}")
        if path.exists() and not force:
            raise ConfigError(
                f"configuration already exists: {path}. Use --force to replace it"
            )
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".config.", delete=False
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(content)
            temporary.flush()
            os.fchmod(temporary.fileno(), 0o600)
        os.replace(temporary_name, path)
    except ConfigError:
        raise
    except OSError as error:
        raise ConfigError(f"cannot write configuration {path}: {error}") from error
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
