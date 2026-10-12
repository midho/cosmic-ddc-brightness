# Changelog

## 0.1.0 - 2026-10-12

### Added

- Control a GeekMagic SmallTV-Ultra through the same target selector, popup,
  brightness steps, and all-displays mode as DDC/CI monitors.
- Toggle GeekMagic between 0% and its remembered nonzero brightness with the
  `toggle-geekmagic` command.
- Reserve GeekMagic 0% for the power toggle: brightness steps stop at 1%, move
  from 1% to 5% on increase, and do not wake an off display.
- Treat simultaneous brightness-down and brightness-up commands as one
  latched target-cycle chord without applying either brightness change.
- Validate the configured GeekMagic model before every local-network read and
  write, and report its status in `doctor`.
- Clean-user installation tests that run the installed launcher and verify
  configuration, diagnostics, custom XDG folders, update replacement,
  uninstall, and purge.
- Full-command tests for guarded fast writes when a monitor moves and another
  monitor takes its old bus.
- Optional popup copies on every connected monitor.
- Percentage-based brightness control for multiple external DDC/CI monitors.
- Stable monitor matching by EDID manufacturer, model, and serial number.
- Automatic recovery when a monitor moves to another I2C bus.
- Tap, hold, release, target selection, and status commands.
- Optional COSMIC-friendly Wayland popup with dynamic monitor rows.
- Configuration and read-only diagnostic commands.
- User service, installer, uninstaller, integration guides, and tests.

### Fixed

- Stop held-key repeats when opposite brightness presses form a target-cycle
  chord. Repeated presses during the chord latch cannot restart them.
- Report malformed GeekMagic addresses as configuration errors instead of
  showing a traceback.
- Avoid repeated display scans for unplugged configured monitors while the
  kernel's output records are unchanged, and retry when an output changes.
- Serialize display scans, brightness reads, and writes so they do not compete
  for a monitor. Use guarded bus reads in the existing fast mode.
- Keep the popup percentage at the last completed write or read while a newer
  brightness target is still queued.

- Show a clear configuration error when the file cannot be read or written.
- Report configuration files that are not valid UTF-8 instead of showing a
  Python traceback.
- Keep the data and configuration paths chosen during installation. The user
  service now finds an installation in custom XDG folders even when those
  environment variables are not present in the service manager. When a custom
  configuration folder is used, the service file is installed in both that
  folder and the standard user-manager folder.
- Keep the small installed-path record when uninstalling without `--purge`.
  A later purge can still find a custom configuration without the original XDG
  environment.
- Make a nonzero brightness step move by at least one raw VCP unit unless the
  monitor is already at the requested limit.
- Bound popup shutdown waits and handle a popup that exits just before it is
  stopped. Popup cleanup can no longer interrupt service cleanup.
- Give popup writes a deadline and drop oversized updates. A popup that stops
  reading is stopped and briefly kept from restarting. A popup that exits
  normally can start again on the next action.
- Add presses received during a fresh monitor read and apply them once to the
  value it returns. The second press used to step from a stale cached value.
- Read a blocked monitor again before applying the next step. Its unconfirmed
  target is not a current brightness reading.
- Stop and reap a running `ddcutil` child when its task is cancelled. Cleanup
  has a deadline after cancellation or timeout, including when a grandchild
  keeps an output descriptor open. Service shutdown waits for its workers, and
  an unexpected worker failure stops the others.
- Ignore the personal `config.toml` file and document that it can contain
  monitor serial numbers.
- Remove generated Python bytecode for known application modules during
  uninstall without deleting unrelated files or saved configuration.
- Select the monitor by manufacturer, model, and serial on every read and on
  default-mode writes. Scan connected identities before default-mode writes,
  with one scan shared by overlapping writes.
- Avoid repeated full scans on successful reads and use the DDC timing options
  that worked better with the earlier personal controller.
- Refuse control without a readable configured serial, clear stale matches after
  failed detection, and handle newly ambiguous identities as recoverable errors.
- Recreate the popup after its display time instead of keeping a hidden Wayland
  surface that can become stale.
- Show an estimated percentage during a brightness change instead of hiding
  it behind "Updating...". Keep the warning when a change cannot be confirmed.
- Let users set `refresh_after_seconds = 0` to skip idle rereads when quick
  keys matter more than tracking changes made outside the controller.
- Keep only the latest held-key target while a monitor write is running, and
  ignore verification reads overtaken by a newer key request.
- Add an optional faster write path that checks the Linux connector EDID and
  bus path before a direct bus write. It falls back to identity selection when
  that check is unavailable. The default still scans before each write.
