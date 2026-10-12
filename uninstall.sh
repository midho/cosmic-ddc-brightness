#!/bin/sh
set -eu

purge=0
if [ "${1:-}" = "--purge" ]; then
    purge=1
    shift
fi
if [ "$#" -ne 0 ]; then
    printf '%s\n' "Usage: ./uninstall.sh [--purge]" >&2
    exit 2
fi

config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
bin_file="$HOME/.local/bin/cosmic-ddc-brightness"
state_dir="$HOME/.local/state/cosmic-ddc-brightness"
paths_file="$state_dir/installed-paths"
app_dir="$data_home/cosmic-ddc-brightness"
config_file="$config_home/cosmic-ddc-brightness/config.toml"

if [ -r "$paths_file" ]; then
    installed_app_dir=
    installed_config_file=
    {
        IFS= read -r installed_app_dir
        IFS= read -r installed_config_file
    } < "$paths_file" || {
        printf '%s\n' "Cannot read installed paths from $paths_file" >&2
        exit 1
    }
    case "$installed_app_dir" in
        /*/cosmic-ddc-brightness) app_dir=$installed_app_dir ;;
        *) printf '%s\n' "Unsafe installed program path: $installed_app_dir" >&2; exit 1 ;;
    esac
    case "$installed_config_file" in
        /*/cosmic-ddc-brightness/config.toml) config_file=$installed_config_file ;;
        *) printf '%s\n' "Unsafe installed configuration path: $installed_config_file" >&2; exit 1 ;;
    esac
fi

package_dir="$app_dir/cosmic_ddc_brightness"
cache_dir="$package_dir/__pycache__"
config_dir=$(dirname -- "$config_file")
installed_config_home=$(dirname -- "$config_dir")
installed_unit_file="$installed_config_home/systemd/user/cosmic-ddc-brightness.service"
default_unit_file="$HOME/.config/systemd/user/cosmic-ddc-brightness.service"

systemctl --user disable --now cosmic-ddc-brightness.service >/dev/null 2>&1 || true

rm -f -- "$bin_file" "$installed_unit_file" "$default_unit_file"
for module_name in \
    __init__ __main__ cli config controller ddc devices errors geekmagic popup runtime; do
    rm -f -- "$package_dir/$module_name.py" "$package_dir/$module_name.pyc"
    for cache_file in "$cache_dir/$module_name".*.pyc; do
        if [ -e "$cache_file" ] || [ -L "$cache_file" ]; then
            rm -f -- "$cache_file"
        fi
    done
done
rmdir -- "$cache_dir" 2>/dev/null || true
rmdir -- "$package_dir" 2>/dev/null || true
rmdir -- "$app_dir" 2>/dev/null || true

if [ "$purge" -eq 1 ]; then
    rm -f -- "$config_file" "$paths_file"
    rmdir -- "$config_dir" 2>/dev/null || true
    rmdir -- "$state_dir" 2>/dev/null || true
    printf '%s\n' "Removed the program, service, and configuration."
else
    printf '%s\n' "Removed the program and service. The configuration and its path record were kept."
fi

systemctl --user daemon-reload
