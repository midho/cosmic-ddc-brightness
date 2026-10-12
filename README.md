# COSMIC Display Brightness

Change the hardware brightness of external displays from COSMIC shortcuts.
`ddcutil` talks to DDC/CI monitors, while the optional GeekMagic backend uses
the SmallTV-Ultra local HTTP interface. A user service handles key presses and
can show a Wayland popup.

## Early release scope

Version 0.1.0 was prepared for Pop!_OS 24.04 running COSMIC on Wayland. It
controls external DDC/CI monitors with readable EDID identities and can also
control one GeekMagic SmallTV-Ultra on the local network. COSMIC shortcuts,
Input Remapper, and Solaar can send commands. This early release has not been
checked across every Linux desktop, monitor, dock, or input device.

Laptop panels do not use DDC/CI. Some docks, KVM switches, adapters, and
monitor modes block DDC/CI even when the monitor supports it.

## Why monitor identity matters

An I2C bus number can change when you reboot, move a cable, or reconnect a
monitor. The configuration records the EDID manufacturer, model, and serial
number for each monitor. By default, the service checks for unique matches
when it starts and before each write. Reads select the saved identity directly,
then rescan if that read fails. The default write command also selects by identity.
If another monitor takes the old bus number and still responds, it will not
receive the brightness change.

At startup, detection stops if the serial number is missing or two monitors
report the same identity. The default setting repeats that check before every
write. A copied or generic serial cannot tell two monitors apart.

With the default setting, held-key writes get a fresh identity check on every
step. Writes requested at the same time can share one scan. This costs time.
The delay depends on your monitors and connection. Unplugging a monitor during
a DDC transaction can interrupt that transaction.

## Install on Pop!_OS 24.04

Install the required packages:

```bash
sudo apt install ddcutil python3-gi gir1.2-gtk-3.0 gir1.2-gtklayershell-0.1
```

Download the project:

```bash
git clone https://github.com/midho/cosmic-ddc-brightness.git
cd cosmic-ddc-brightness
```

Make sure DDC/CI is enabled in each monitor's own menu. Then run:

```bash
./install.sh
```

The installer writes to your user folders. On a first install, it detects
monitors, saves a configuration, and enables the user service. It does not run
`sudo`, edit desktop shortcuts, or overwrite a configuration you already have.
If `XDG_DATA_HOME` or `XDG_CONFIG_HOME` points somewhere else during
installation, the launcher records those paths. The user service then uses the
same files even if it does not inherit those environment variables. For a
custom configuration folder, the installer puts the service file there and in
`~/.config/systemd/user`. This also works when the already-running service
manager still uses the standard folder.

Run the health check:

```bash
cosmic-ddc-brightness doctor
```

For a first setup, `configure` detects connected DDC/CI monitors and writes
their identities to the configuration. It will stop if that file already
exists. Read its message before continuing. `configure --force` replaces the
whole file, including custom settings and any GeekMagic entry, so make a copy
before using it.

To add a monitor later, connect it and check its EDID identity with
`ddcutil detect`. Add a `[[monitors]]` entry to `config.toml` with its
manufacturer, model, serial, and a unique ID. Make a copy of the file before
editing, then restart the service. A new monitor is not added to the saved
list automatically.

If first-time configuration was skipped or could not find a monitor:

```bash
cosmic-ddc-brightness configure
systemctl --user enable --now cosmic-ddc-brightness.service
```

## Commands

```text
cosmic-ddc-brightness up
cosmic-ddc-brightness down
cosmic-ddc-brightness press-up
cosmic-ddc-brightness release-up
cosmic-ddc-brightness press-down
cosmic-ddc-brightness release-down
cosmic-ddc-brightness stop
cosmic-ddc-brightness show
cosmic-ddc-brightness cycle-monitor
cosmic-ddc-brightness toggle-geekmagic
```

`up` and `down` make one change. The `press-*` and `release-*` commands handle
held keys. `cycle-monitor` moves from all displays through the configured
devices in order. A newly started service selects all displays.
`toggle-geekmagic` turns the configured GeekMagic display off at 0% and restores
its last nonzero brightness when pressed again.

For GeekMagic, 0% is reserved for that explicit off/on toggle. Ordinary
brightness changes stop at 1%. Increasing from 1% moves to 5%, then continues
in the configured steps. Brightness commands leave an off GeekMagic display
off. Opposite brightness commands received together act as one target-cycle
chord and do not change brightness.

## Configuration

The default file is:

```text
~/.config/cosmic-ddc-brightness/config.toml
```

Use `config.example.toml` as a reference. These settings are worth checking:

- `step_percent`: brightness change per step. It is scaled to each monitor's
  VCP maximum, so five percent is still five percent when the maximum is 254.
  A nonzero step always moves by at least one raw VCP unit unless the monitor
  is already at the requested limit.
- A saved monitor that is unplugged shows as unavailable. When Linux provides
  complete connection records, repeated key presses do not rescan it until an
  output changes. The service redetects saved monitor identities at startup
  and when a later command needs a fresh match. A reconnected configured
  monitor uses its saved entry. A new monitor needs its own configuration
  entry.
- The popup percentage follows completed writes, then loses its approximation
  mark when the monitor confirms the value. Queued targets do not move the bar
  ahead of the hardware.
- `scan_before_each_write`: the default is `true`. The service checks for
  missing or duplicate monitor identities before each write. Set it to `false`
  only if those scans make held keys too slow. On Linux systems with usable
  connector identity files, the service checks the monitor's EDID, connection
  state, and bus path immediately before direct bus reads and writes. If it
  cannot set up that check, it falls back to slower EDID-selected commands. A changed
  identity or bus blocks the direct write and triggers fresh detection. This
  avoids writing to a different monitor that takes the old bus in the tested
  cases, but it cannot make a hotplug race impossible. A newly connected
  monitor with the same EDID identity may also go unnoticed until the next
  full scan. The startup scan rejects duplicates it finds, and `doctor` warns
  when this faster setting is in use.
- Held keys update the requested brightness at the configured repeat rate.
  Each monitor writes only its latest target, so slow writes do not form a
  separate queue for every key repeat. A write already running when the key
  is released may still finish afterward.
- `refresh_after_seconds`: read an idle monitor before applying a new change.
  If you used the monitor's own buttons, the next step starts from the new
  reading. Key presses received during that read are added together and applied
  once. A shorter interval catches outside changes sooner but makes the first
  keypress after a pause slower. A longer interval responds sooner, but a
  monitor-button change may not be noticed until the next read. Set it to `0`
  to skip automatic idle rereads and use the last confirmed level, like a
  typical brightness key. Startup, an explicit `show`, and error recovery
  still read the monitor. With `0`, a change made with the monitor's own
  buttons can make the next step jump from the old saved level. The popup
  marks its target with `≈` until the monitor confirms it.
- `popup.monitor_model`: optional exact GTK monitor model for popup placement.
  Leave it empty to let COSMIC choose the display. It is ignored when
  `popup.all_monitors` is `true`.
- `popup.all_monitors`: show an identical popup on every connected monitor.
  The default is `false`.
- `[[monitors]]`: stable EDID identity and a friendly display name.
- `[[geekmagic]]`: friendly name and local HTTP address for an optional
  GeekMagic SmallTV-Ultra. The address is checked against `/v.json` before
  every read and write.

Each configured identity must match one connected monitor. Missing serials or
ambiguous matches found at startup stop control. With the default write scan,
they also stop later changes.

Your configuration may contain monitor serial numbers. Keep it private. Git
ignores `config.toml` in this repository.

Example GeekMagic entry:

```toml
[[geekmagic]]
id = "geekmagic-ultra"
name = "GeekMagic Ultra"
url = "http://192.0.2.10" # Example only. Replace with your GeekMagic address.
timeout_seconds = 1.5
restore_brightness = 50
```

The GeekMagic address should be stable, for example through a router DHCP
reservation. The display's web interface has no visible authentication, so
keep it on a trusted local network.

After a configuration change:

```bash
systemctl --user restart cosmic-ddc-brightness.service
```

## Connect keyboard controls

- [COSMIC shortcuts](docs/integrations/cosmic.md)
- [Input Remapper and Vaydeer](docs/integrations/input-remapper.md)
- [Solaar press and release rules](docs/integrations/solaar.md)

The installer does not edit those applications' configuration files.

## Troubleshooting

```bash
cosmic-ddc-brightness doctor
systemctl --user status cosmic-ddc-brightness.service --no-pager
journalctl --user -u cosmic-ddc-brightness.service -n 50 --no-pager
ddcutil environment
```

If no monitor is found:

- Enable DDC/CI in the monitor menu.
- Confirm the `i2c-dev` kernel module is loaded.
- Confirm your user can read and write the monitor's `/dev/i2c-*` device.
- Try a direct connection if a dock, KVM, or adapter is in the path.

The popup exits when its display timer ends. The next action creates a fresh
Wayland surface, which helps after a display or compositor change. Popup writes
have a short deadline. An oversized update is dropped, and a popup that stops
reading cannot hold up brightness control. Popup shutdown also has a deadline,
so a slow child-process watcher cannot hold up service shutdown.

## Update and remove

Run `./install.sh` again to update program files. Your configuration is kept.

Remove the program and service while keeping the configuration:

```bash
./uninstall.sh
```

The uninstaller removes the installed program files and their Python cache
files. It keeps your configuration and unrelated files in the same directory.
It also keeps a small local record of the configuration path, so a later
`--purge` can find a custom configuration without the old XDG environment.

Also remove the configuration:

```bash
./uninstall.sh --purge
```

## Development

Run the tests without touching monitor hardware:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
bash -n install.sh uninstall.sh scripts/cosmic-ddc-brightness
```

The tests use a fake `ddcutil`, so they do not change your monitors. They cover
taps, holds, slow writes, blocked monitors, and presses received during a read.
Subprocess tests check cancellation and timeout cleanup with a background child
holding output descriptors open. The daemon test stops the service during a
read and checks for leftover processes or sockets. Popup tests use a child that
stays alive but stops reading, and check oversized updates. Installer tests run
the copied launcher in a temporary user folder, then check configuration,
custom XDG folders, diagnostics, update, uninstall, and purge. A full-command
test checks the guarded direct-bus path when a monitor moves and a replacement
takes its old bus. A GPU, cable, adapter, or monitor can still behave
differently in a real DDC session.

## License

MIT. `ddcutil` is a separate program and is licensed under GPLv2.
