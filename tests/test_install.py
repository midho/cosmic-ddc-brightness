import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.bin = self.root / "fake-bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.systemctl_log = self.root / "systemctl.log"

        systemctl = self.bin / "systemctl"
        systemctl.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            printf '%s\\n' "$*" >> {str(self.systemctl_log)!r}
        """), encoding="utf-8")
        systemctl.chmod(0o755)

        ddcutil = self.bin / "ddcutil"
        ddcutil.write_text(textwrap.dedent("""\
            #!/bin/sh
            if [ "$*" = "detect --brief" ]; then
                printf '%s\\n' \\
                    'Display 1' \\
                    '   I2C bus:             /dev/i2c-9' \\
                    '   Monitor:             DEL:U2720Q:TEST123'
                exit 0
            fi
            if [ "$*" = "--version" ]; then
                printf '%s\\n' 'ddcutil 2.2.7'
                exit 0
            fi
            if printf '%s' "$*" | grep -q 'getvcp 10'; then
                printf '%s\\n' 'VCP 10 C 50 100'
                exit 0
            fi
            exit 2
        """), encoding="utf-8")
        ddcutil.chmod(0o755)

        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def run_script(self, name, *arguments):
        return subprocess.run(
            [str(PROJECT / name), *arguments],
            cwd=PROJECT,
            env=self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_install_update_uninstall_and_purge(self):
        first = self.run_script("install.sh")
        self.assertEqual(first.returncode, 0, first.stderr)

        config = self.home / ".config/cosmic-ddc-brightness/config.toml"
        launcher = self.home / ".local/bin/cosmic-ddc-brightness"
        service = self.home / ".config/systemd/user/cosmic-ddc-brightness.service"
        self.assertTrue(config.is_file())
        self.assertTrue(launcher.is_file())
        self.assertTrue(service.is_file())
        version = subprocess.run(
            [launcher, "--version"],
            cwd=PROJECT,
            env=self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(version.returncode, 0, version.stderr)
        original_config = config.read_text(encoding="utf-8")
        self.assertIn('serial = "TEST123"', original_config)

        installed_cli = (
            self.home
            / ".local/share/cosmic-ddc-brightness/cosmic_ddc_brightness/cli.py"
        )
        installed_cli.write_text("old installed file\n", encoding="utf-8")
        second = self.run_script("install.sh")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(config.read_text(encoding="utf-8"), original_config)
        self.assertEqual(
            installed_cli.read_text(encoding="utf-8"),
            (PROJECT / "src/cosmic_ddc_brightness/cli.py").read_text(encoding="utf-8"),
        )
        doctor = subprocess.run(
            [launcher, "doctor"],
            cwd=PROJECT,
            env=self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertIn("Result: ready", doctor.stdout)
        service_calls = self.systemctl_log.read_text(encoding="utf-8")
        self.assertIn("--user restart cosmic-ddc-brightness.service", service_calls)

        unrelated = config.parent / "keep.txt"
        unrelated.write_text("keep", encoding="utf-8")
        removed = self.run_script("uninstall.sh")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertTrue(config.is_file())
        self.assertTrue(unrelated.is_file())
        self.assertFalse(launcher.exists())
        self.assertFalse(service.exists())

        purged = self.run_script("uninstall.sh", "--purge")
        self.assertEqual(purged.returncode, 0, purged.stderr)
        self.assertFalse(config.exists())
        self.assertTrue(unrelated.is_file())

    def test_uninstall_removes_generated_bytecode_and_keeps_unrelated_files(self):
        installed = self.run_script("install.sh")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        app_dir = self.home / ".local/share/cosmic-ddc-brightness"
        package_dir = app_dir / "cosmic_ddc_brightness"
        environment = dict(self.environment)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        environment["PYTHONPATH"] = str(app_dir)
        imported = subprocess.run(
            [sys.executable, "-c", "import cosmic_ddc_brightness.cli"],
            cwd=self.root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertTrue(list((package_dir / "__pycache__").glob("*.pyc")))
        unrelated = app_dir / "keep.txt"
        unrelated.write_text("keep", encoding="utf-8")

        removed = self.run_script("uninstall.sh")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(package_dir.exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_custom_xdg_paths_work_without_service_environment(self):
        custom_data = self.root / "custom data"
        custom_config = self.root / "custom config"
        self.environment["XDG_DATA_HOME"] = str(custom_data)
        self.environment["XDG_CONFIG_HOME"] = str(custom_config)

        installed = self.run_script("install.sh")
        self.assertEqual(installed.returncode, 0, installed.stderr)

        launcher = self.home / ".local/bin/cosmic-ddc-brightness"
        paths = self.home / ".local/state/cosmic-ddc-brightness/installed-paths"
        config = custom_config / "cosmic-ddc-brightness/config.toml"
        custom_service = custom_config / "systemd/user/cosmic-ddc-brightness.service"
        manager_service = (
            self.home / ".config/systemd/user/cosmic-ddc-brightness.service"
        )
        self.assertEqual(
            paths.read_text(encoding="utf-8").splitlines(),
            [str(custom_data / "cosmic-ddc-brightness"), str(config)],
        )
        self.assertTrue(custom_service.is_file())
        self.assertTrue(manager_service.is_file())

        service_environment = dict(self.environment)
        service_environment.pop("XDG_DATA_HOME")
        service_environment.pop("XDG_CONFIG_HOME")
        doctor = subprocess.run(
            [launcher, "doctor"],
            cwd=PROJECT,
            env=service_environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertIn(f"configuration is valid: {config}", doctor.stdout)

        removed = subprocess.run(
            [str(PROJECT / "uninstall.sh")],
            cwd=PROJECT,
            env=service_environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse((custom_data / "cosmic-ddc-brightness").exists())
        self.assertFalse(custom_service.exists())
        self.assertFalse(manager_service.exists())
        self.assertTrue(config.is_file())
        self.assertTrue(paths.is_file())

        purged = subprocess.run(
            [str(PROJECT / "uninstall.sh"), "--purge"],
            cwd=PROJECT,
            env=service_environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(purged.returncode, 0, purged.stderr)
        self.assertFalse(config.exists())
        self.assertFalse(paths.exists())

    def test_update_reuses_recorded_custom_xdg_paths(self):
        custom_data = self.root / "custom data"
        custom_config = self.root / "custom config"
        self.environment["XDG_DATA_HOME"] = str(custom_data)
        self.environment["XDG_CONFIG_HOME"] = str(custom_config)

        first = self.run_script("install.sh")
        self.assertEqual(first.returncode, 0, first.stderr)

        app_dir = custom_data / "cosmic-ddc-brightness"
        config = custom_config / "cosmic-ddc-brightness/config.toml"
        paths = self.home / ".local/state/cosmic-ddc-brightness/installed-paths"
        installed_cli = app_dir / "cosmic_ddc_brightness/cli.py"
        original_config = config.read_text(encoding="utf-8")
        original_paths = paths.read_text(encoding="utf-8")
        installed_cli.write_text("old installed file\n", encoding="utf-8")

        self.environment.pop("XDG_DATA_HOME")
        self.environment.pop("XDG_CONFIG_HOME")
        second = self.run_script("install.sh")
        self.assertEqual(second.returncode, 0, second.stderr)

        self.assertEqual(paths.read_text(encoding="utf-8"), original_paths)
        self.assertEqual(config.read_text(encoding="utf-8"), original_config)
        self.assertEqual(
            installed_cli.read_text(encoding="utf-8"),
            (PROJECT / "src/cosmic_ddc_brightness/cli.py").read_text(
                encoding="utf-8"
            ),
        )
        self.assertFalse(
            (self.home / ".local/share/cosmic-ddc-brightness").exists()
        )
        self.assertFalse(
            (self.home / ".config/cosmic-ddc-brightness/config.toml").exists()
        )
        doctor = subprocess.run(
            [self.home / ".local/bin/cosmic-ddc-brightness", "doctor"],
            cwd=PROJECT,
            env=self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertIn(f"configuration is valid: {config}", doctor.stdout)
        self.assertIn(
            "--user restart cosmic-ddc-brightness.service",
            self.systemctl_log.read_text(encoding="utf-8"),
        )

    def test_install_rejects_malformed_or_unsafe_path_records(self):
        paths = self.home / ".local/state/cosmic-ddc-brightness/installed-paths"
        paths.parent.mkdir(parents=True)
        cases = (
            ("one line\n", "malformed"),
            (
                "/tmp/cosmic-ddc-brightness\n"
                "/tmp/cosmic-ddc-brightness/config.toml\n"
                "extra\n",
                "malformed",
            ),
            (
                "relative/cosmic-ddc-brightness\n"
                "/tmp/cosmic-ddc-brightness/config.toml\n",
                "Unsafe installed program path",
            ),
            (
                "/tmp/cosmic-ddc-brightness\n"
                "/tmp/../etc/cosmic-ddc-brightness/config.toml\n",
                "unsafe path",
            ),
        )

        for contents, message in cases:
            with self.subTest(contents=contents):
                paths.write_text(contents, encoding="utf-8")
                result = self.run_script("install.sh")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse(
                    (self.home / ".local/share/cosmic-ddc-brightness").exists()
                )


if __name__ == "__main__":
    unittest.main()
