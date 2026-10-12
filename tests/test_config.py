import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cosmic_ddc_brightness.config import (
    ConfigError,
    GeekMagicConfig,
    MonitorConfig,
    load_config,
    parse_config,
    render_config,
    write_config,
)


class ConfigTests(unittest.TestCase):
    def test_geekmagic_is_appended_after_ddc_monitors(self):
        settings = parse_config({
            "version": 1,
            "monitors": [{
                "id": "desk", "name": "Desk", "manufacturer": "DEL",
                "model": "A", "serial": "1",
            }],
            "geekmagic": [{
                "id": "geekmagic", "name": "GeekMagic Ultra",
                "url": "http://192.168.1.20/",
            }],
        })
        self.assertEqual(
            settings.geekmagic,
            (GeekMagicConfig(
                "geekmagic", "GeekMagic Ultra", "http://192.168.1.20"
            ),),
        )
        self.assertEqual([device.id for device in settings.devices], ["desk", "geekmagic"])

    def test_duplicate_id_across_device_types_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "duplicate device id"):
            parse_config({
                "version": 1,
                "monitors": [{
                    "id": "screen", "name": "Desk", "manufacturer": "DEL",
                    "model": "A", "serial": "1",
                }],
                "geekmagic": [{
                    "id": "screen", "name": "GeekMagic",
                    "url": "http://192.168.1.20",
                }],
            })

    def test_geekmagic_rejects_non_http_or_path_url(self):
        monitor = {
            "id": "desk", "name": "Desk", "manufacturer": "DEL",
            "model": "A", "serial": "1",
        }
        for url in ("https://192.168.1.20", "http://192.168.1.20/settings"):
            with self.subTest(url=url), self.assertRaisesRegex(ConfigError, "HTTP device address"):
                parse_config({
                    "version": 1,
                    "monitors": [monitor],
                    "geekmagic": [{
                        "id": "geekmagic", "name": "GeekMagic", "url": url,
                    }],
                })

    def test_geekmagic_rejects_malformed_authority_and_port(self):
        monitor = {
            "id": "desk", "name": "Desk", "manufacturer": "DEL",
            "model": "A", "serial": "1",
        }
        for url in ("http://[bad", "http://host:bad", "http://host:99999"):
            with self.subTest(url=url), self.assertRaisesRegex(ConfigError, "invalid GeekMagic url"):
                parse_config({
                    "version": 1,
                    "monitors": [monitor],
                    "geekmagic": [{
                        "id": "geekmagic", "name": "GeekMagic", "url": url,
                    }],
                })

    def test_geekmagic_accepts_valid_host_and_port(self):
        settings = parse_config({
            "version": 1,
            "monitors": [{
                "id": "desk", "name": "Desk", "manufacturer": "DEL",
                "model": "A", "serial": "1",
            }],
            "geekmagic": [{
                "id": "geekmagic", "name": "GeekMagic",
                "url": "http://device.local:8080/",
            }],
        })
        self.assertEqual(settings.geekmagic[0].url, "http://device.local:8080")

    def test_rendered_config_round_trips(self):
        monitors = [MonitorConfig("dell", "Desk", "DEL", "DELL P2417H", "ABC123")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            write_config(path, render_config(monitors))
            loaded = load_config(path)
        self.assertEqual(loaded.monitors, tuple(monitors))
        self.assertEqual(loaded.step_percent, 5.0)
        self.assertTrue(loaded.scan_before_each_write)
        self.assertFalse(loaded.popup.all_monitors)

    def test_fast_write_mode_is_explicit(self):
        settings = parse_config({
            "version": 1,
            "scan_before_each_write": False,
            "monitors": [{
                "id": "one", "name": "One", "manufacturer": "DEL",
                "model": "A", "serial": "1",
            }],
        })
        self.assertFalse(settings.scan_before_each_write)

    def test_invalid_write_scan_setting_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "scan_before_each_write"):
            parse_config({
                "version": 1,
                "scan_before_each_write": "no",
                "monitors": [{
                    "id": "one", "name": "One", "manufacturer": "DEL",
                    "model": "A", "serial": "1",
                }],
            })

    def test_existing_config_is_preserved(self):
        monitors = [MonitorConfig("one", "One", "DEL", "A", "1")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            write_config(path, render_config(monitors))
            with self.assertRaises(ConfigError):
                write_config(path, render_config(monitors))

    def test_read_filesystem_error_has_a_clear_message(self):
        path = Path("/blocked/config.toml")
        with (
            patch.object(Path, "open", side_effect=PermissionError("access denied")),
            self.assertRaisesRegex(
                ConfigError, r"cannot read configuration /blocked/config\.toml: access denied"
            ),
        ):
            load_config(path)

    def test_invalid_utf8_has_a_clear_message(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_bytes(b"version = 1\nname = \xff\n")
            with self.assertRaisesRegex(
                ConfigError, r"configuration is not valid UTF-8: .*config\.toml"
            ):
                load_config(path)

    def test_write_filesystem_error_has_a_clear_message_and_cleans_up(self):
        monitors = [MonitorConfig("one", "One", "DEL", "A", "1")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            with (
                patch(
                    "cosmic_ddc_brightness.config.os.replace",
                    side_effect=PermissionError("read-only filesystem"),
                ),
                self.assertRaisesRegex(
                    ConfigError, r"cannot write configuration .*read-only filesystem"
                ),
            ):
                write_config(path, render_config(monitors))
            self.assertEqual(list(path.parent.iterdir()), [])

    def test_invalid_range_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "step_percent"):
            parse_config({
                "version": 1,
                "step_percent": 80,
                "monitors": [{
                    "id": "one", "name": "One", "manufacturer": "DEL",
                    "model": "A", "serial": "1",
                }],
            })

    def test_duplicate_identity_is_rejected(self):
        monitor = {
            "id": "one", "name": "One", "manufacturer": "DEL",
            "model": "A", "serial": "1",
        }
        duplicate = {**monitor, "id": "two", "name": "Two"}
        with self.assertRaisesRegex(ConfigError, "same identity"):
            parse_config({"version": 1, "monitors": [monitor, duplicate]})

    def test_all_monitor_popup_setting_is_loaded(self):
        settings = parse_config({
            "version": 1,
            "popup": {"all_monitors": True},
            "monitors": [{
                "id": "one", "name": "One", "manufacturer": "DEL",
                "model": "A", "serial": "1",
            }],
        })
        self.assertTrue(settings.popup.all_monitors)

    def test_non_boolean_all_monitor_setting_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "all_monitors"):
            parse_config({
                "version": 1,
                "popup": {"all_monitors": "yes"},
                "monitors": [{
                    "id": "one", "name": "One", "manufacturer": "DEL",
                    "model": "A", "serial": "1",
                }],
            })


if __name__ == "__main__":
    unittest.main()
