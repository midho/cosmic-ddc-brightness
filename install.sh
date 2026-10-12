#!/bin/sh
set -eu

enable_service=1
configure=1

usage() {
    printf '%s\n' "Usage: ./install.sh [--no-enable] [--no-configure]"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-enable) enable_service=0 ;;
        --no-configure) configure=0 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
    shift
done

for command_name in install mktemp systemctl /usr/bin/python3; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        printf '%s\n' "Required command is missing: $command_name" >&2
        exit 1
    fi
done

if ! command -v ddcutil >/dev/null 2>&1; then
    printf '%s\n' "ddcutil is required. On Pop!_OS: sudo apt install ddcutil" >&2
    exit 1
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bin_dir="$HOME/.local/bin"
default_unit_dir="$HOME/.config/systemd/user"
state_dir="$HOME/.local/state/cosmic-ddc-brightness"
paths_file="$state_dir/installed-paths"

if [ -e "$paths_file" ] || [ -L "$paths_file" ]; then
    if [ ! -f "$paths_file" ] || [ ! -r "$paths_file" ]; then
        printf '%s\n' "Installed path record is not a readable file: $paths_file" >&2
        exit 1
    fi

    app_dir=
    config_file=
    extra_line=
    if ! {
        IFS= read -r app_dir &&
        IFS= read -r config_file &&
        ! IFS= read -r extra_line
    } < "$paths_file"; then
        printf '%s\n' \
            "Installed path record is malformed; expected exactly two lines: $paths_file" >&2
        exit 1
    fi
else
    data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
    config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
    app_dir="$data_home/cosmic-ddc-brightness"
    config_file="$config_home/cosmic-ddc-brightness/config.toml"
fi

case "$app_dir" in
    /*/cosmic-ddc-brightness) ;;
    *) printf '%s\n' "Unsafe installed program path in $paths_file" >&2; exit 1 ;;
esac
case "$config_file" in
    /*/cosmic-ddc-brightness/config.toml) ;;
    *) printf '%s\n' "Unsafe installed configuration path in $paths_file" >&2; exit 1 ;;
esac
case "$app_dir:$config_file" in
    *//*|*/./*|*/../*|*/.|*/..)
        printf '%s\n' "Installed path record contains an unsafe path: $paths_file" >&2
        exit 1
        ;;
esac

config_dir=$(dirname -- "$config_file")
installed_config_home=$(dirname -- "$config_dir")
unit_dir="$installed_config_home/systemd/user"

install -d -m 0755 \
    "$app_dir/cosmic_ddc_brightness" "$bin_dir" "$unit_dir" "$default_unit_dir"
install -d -m 0700 "$state_dir"

for source_file in "$script_dir"/src/cosmic_ddc_brightness/*.py; do
    install -m 0644 "$source_file" "$app_dir/cosmic_ddc_brightness/$(basename -- "$source_file")"
done

install -m 0755 "$script_dir/scripts/cosmic-ddc-brightness" "$bin_dir/cosmic-ddc-brightness"
paths_temp=$(mktemp "$paths_file.tmp.XXXXXX")
trap 'rm -f -- "$paths_temp"' EXIT HUP INT TERM
printf '%s\n%s\n' "$app_dir" "$config_file" > "$paths_temp"
mv -f -- "$paths_temp" "$paths_file"
trap - EXIT HUP INT TERM
install -m 0644 \
    "$script_dir/systemd/cosmic-ddc-brightness.service" \
    "$default_unit_dir/cosmic-ddc-brightness.service"
if [ "$unit_dir" != "$default_unit_dir" ]; then
    install -m 0644 \
        "$script_dir/systemd/cosmic-ddc-brightness.service" \
        "$unit_dir/cosmic-ddc-brightness.service"
fi

systemctl --user daemon-reload

if [ ! -e "$config_file" ] && [ "$configure" -eq 1 ]; then
    if ! "$bin_dir/cosmic-ddc-brightness" configure; then
        printf '%s\n' "Monitor detection did not create a configuration." >&2
        printf '%s\n' "Fix the reported problem, then run: cosmic-ddc-brightness configure" >&2
        enable_service=0
    fi
fi

if [ "$enable_service" -eq 1 ]; then
    if [ ! -f "$config_file" ]; then
        printf '%s\n' "No configuration exists, so the service was not enabled." >&2
        exit 1
    fi
    systemctl --user enable cosmic-ddc-brightness.service
    systemctl --user restart cosmic-ddc-brightness.service
    printf '%s\n' "Installed and started cosmic-ddc-brightness."
else
    printf '%s\n' "Installed cosmic-ddc-brightness without starting the service."
fi

printf '%s\n' "Run: cosmic-ddc-brightness doctor"
