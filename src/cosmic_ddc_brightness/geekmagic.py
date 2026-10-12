from __future__ import annotations

import asyncio
import json
import os
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from .config import GeekMagicConfig
from .errors import DeviceCommandError


MAX_RESPONSE_BYTES = 4096
MODEL = "SmallTV-Ultra"


class GeekMagicClient:
    def __init__(self, config: GeekMagicConfig):
        self.config = config
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    async def request(self, path: str) -> str:
        try:
            return await asyncio.to_thread(self._request, path)
        except DeviceCommandError:
            raise
        except (OSError, UnicodeError) as error:
            raise DeviceCommandError(
                f"cannot contact GeekMagic at {self.config.url}: {error}"
            ) from error

    def _request(self, path: str) -> str:
        request = urllib.request.Request(
            f"{self.config.url}{path}",
            headers={"Cache-Control": "no-cache", "Connection": "close"},
        )
        try:
            with self.opener.open(request, timeout=self.config.timeout_seconds) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except OSError as error:
            raise DeviceCommandError(
                f"cannot contact GeekMagic at {self.config.url}: {error}"
            ) from error
        if len(payload) > MAX_RESPONSE_BYTES:
            raise DeviceCommandError("GeekMagic returned an oversized response")
        try:
            return payload.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as error:
            raise DeviceCommandError("GeekMagic returned invalid text") from error

    async def identity(self) -> tuple[str, str]:
        try:
            payload = json.loads(await self.request("/v.json"))
            model = payload["m"]
            firmware = payload["v"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise DeviceCommandError("GeekMagic returned invalid device information") from error
        if model != MODEL:
            raise DeviceCommandError(
                f"configured address is {model!r}, not a {MODEL}"
            )
        if not isinstance(firmware, str):
            raise DeviceCommandError("GeekMagic returned an invalid firmware version")
        return model, firmware

    async def read_brightness(self) -> tuple[int, int]:
        await self.identity()
        try:
            payload = json.loads(await self.request("/brt.json"))
            value = int(payload["brt"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise DeviceCommandError("GeekMagic returned invalid brightness data") from error
        if value == -10:
            value = 0
        if not 0 <= value <= 100:
            raise DeviceCommandError(f"GeekMagic returned invalid brightness {value}")
        return value, 100

    async def write_brightness(self, value: int) -> None:
        if not 0 <= value <= 100:
            raise DeviceCommandError(f"invalid GeekMagic brightness {value}")
        await self.identity()
        query = urllib.parse.urlencode({"brt": value})
        if await self.request(f"/set?{query}") != "OK":
            raise DeviceCommandError("GeekMagic rejected the brightness change")

    def last_brightness_path(self) -> Path:
        state_home = Path(
            os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
        )
        return state_home / "geekmagic-brightness" / "last-on-brightness"

    def load_last_brightness(self) -> int:
        try:
            value = int(self.last_brightness_path().read_text(encoding="utf-8").strip())
        except (FileNotFoundError, OSError, UnicodeError, ValueError):
            return self.config.restore_brightness
        if 0 < value <= 100:
            return value
        return self.config.restore_brightness

    def save_last_brightness(self, value: int) -> None:
        path = self.last_brightness_path()
        temporary_name = ""
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(f"{value}\n")
                temporary.flush()
                os.fchmod(temporary.fileno(), 0o600)
            os.replace(temporary_name, path)
        except OSError as error:
            raise DeviceCommandError(f"cannot remember GeekMagic brightness: {error}") from error
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def toggle_target(self, current: int) -> int:
        if current == 0:
            return self.load_last_brightness()
        self.save_last_brightness(current)
        return 0
