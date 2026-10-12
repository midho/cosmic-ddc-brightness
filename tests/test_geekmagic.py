import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from cosmic_ddc_brightness.config import GeekMagicConfig
from cosmic_ddc_brightness.devices import DeviceManager
from cosmic_ddc_brightness.errors import DeviceCommandError
from cosmic_ddc_brightness.geekmagic import GeekMagicClient


class GeekMagicClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self) -> GeekMagicClient:
        return GeekMagicClient(GeekMagicConfig(
            "geekmagic", "GeekMagic Ultra", "http://192.168.1.20"
        ))

    async def test_reads_off_sentinel_as_zero(self):
        client = self.client()
        client.request = AsyncMock(side_effect=[
            json.dumps({"m": "SmallTV-Ultra", "v": "Ultra-V9.0.50"}),
            json.dumps({"brt": "-10"}),
        ])
        self.assertEqual(await client.read_brightness(), (0, 100))
        self.assertEqual(
            [call.args[0] for call in client.request.await_args_list],
            ["/v.json", "/brt.json"],
        )

    async def test_write_validates_model_and_encodes_value(self):
        client = self.client()
        client.request = AsyncMock(side_effect=[
            json.dumps({"m": "SmallTV-Ultra", "v": "Ultra-V9.0.50"}),
            "OK",
        ])
        await client.write_brightness(45)
        self.assertEqual(
            [call.args[0] for call in client.request.await_args_list],
            ["/v.json", "/set?brt=45"],
        )

    async def test_wrong_model_is_rejected_before_write(self):
        client = self.client()
        client.request = AsyncMock(return_value=json.dumps({"m": "Other", "v": "1"}))
        with self.assertRaisesRegex(DeviceCommandError, "not a SmallTV-Ultra"):
            await client.write_brightness(45)

    def test_toggle_state_is_persistent(self):
        client = self.client()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_STATE_HOME": directory}
        ):
            self.assertEqual(client.toggle_target(35), 0)
            self.assertEqual(client.last_brightness_path().read_text().strip(), "35")
            replacement = self.client()
            self.assertEqual(replacement.toggle_target(0), 35)


class FakeDdc:
    def __init__(self):
        self.writes = []

    async def read(self, _device_id):
        return (25, 100)

    async def write(self, device_id, value):
        self.writes.append((device_id, value))


class FakeGeekMagic:
    def __init__(self):
        self.writes = []

    async def read_brightness(self):
        return (10, 100)

    async def write_brightness(self, value):
        self.writes.append(value)

    def toggle_target(self, current):
        return 0 if current else 50


class DeviceManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_each_device_to_its_backend(self):
        ddc = FakeDdc()
        geekmagic = FakeGeekMagic()
        manager = DeviceManager(ddc, {"geekmagic": geekmagic})
        self.assertEqual(await manager.read("desk"), (25, 100))
        self.assertEqual(await manager.read("geekmagic"), (10, 100))
        await manager.write("desk", 30)
        await manager.write("geekmagic", 15)
        self.assertEqual(ddc.writes, [("desk", 30)])
        self.assertEqual(geekmagic.writes, [15])
        self.assertEqual(manager.toggle_target("geekmagic", 10), 0)


if __name__ == "__main__":
    unittest.main()
