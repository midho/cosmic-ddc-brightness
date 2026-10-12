from __future__ import annotations

from .ddc import DdcManager
from .errors import DeviceCommandError
from .geekmagic import GeekMagicClient


class DeviceManager:
    def __init__(
        self,
        ddc: DdcManager,
        geekmagic: dict[str, GeekMagicClient] | None = None,
    ):
        self.ddc = ddc
        self.geekmagic = geekmagic or {}

    async def read(self, device_id: str) -> tuple[int, int]:
        client = self.geekmagic.get(device_id)
        if client is not None:
            return await client.read_brightness()
        return await self.ddc.read(device_id)

    async def write(self, device_id: str, value: int) -> None:
        client = self.geekmagic.get(device_id)
        if client is not None:
            await client.write_brightness(value)
            return
        await self.ddc.write(device_id, value)

    def toggle_target(self, device_id: str, current: int) -> int:
        client = self.geekmagic.get(device_id)
        if client is None:
            raise DeviceCommandError(f"device {device_id!r} does not support toggling")
        return client.toggle_target(current)
