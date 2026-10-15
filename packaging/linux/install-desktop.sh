#!/bin/sh
# Optional, create-only shortcut. No root, autostart or application launch.
set -eu
fail() { printf 'Keivotos: %s\n' "$1" >&2; exit "$2"; }
entry=$0
if command -v readlink >/dev/null 2>&1; then entry=$(readlink -f -- "$entry"); fi
app_dir=$(CDPATH= cd -- "$(dirname -- "$entry")" && pwd -P)
name=Keivotos
launcher=start-keivotos.sh
filename=Keivotos.desktop
case "${1:-}" in
    '') [ "$#" -eq 0 ] || fail 'Use install-desktop.sh [--portable].' 64 ;;
    --portable) [ "$#" -eq 1 ] || fail 'Use install-desktop.sh [--portable].' 64
        name='Keivotos (portable)'; launcher=start-portable.sh; filename=Keivotos-portable.desktop ;;
    *) fail 'Use install-desktop.sh [--portable].' 64 ;;
esac
data_home=${XDG_DATA_HOME:-${HOME:?HOME must be set}/.local/share}
case "$data_home" in /*) ;; *) fail 'XDG_DATA_HOME must be an absolute path.' 64 ;; esac
carriage_return=$(printf '\r')
case "$app_dir$data_home" in *'
'*|*"$carriage_return"*) fail 'Desktop installation does not support line breaks in paths.' 64 ;; esac
[ -f "$app_dir/$launcher" ] && [ -f "$app_dir/Keivotos.desktop.in" ] || fail 'Extract the entire application folder before installing its shortcut.' 66
destination="$data_home/applications"
mkdir -p -- "$destination" || fail "Cannot create $destination." 73
target="$destination/$filename"
[ ! -e "$target" ] && [ ! -L "$target" ] || fail "Shortcut already exists: $target. It was left unchanged." 73

# Desktop entries first decode string escapes, then quoted Exec arguments.
exec_path=$(printf '%s' "$app_dir/$launcher" | awk '
    BEGIN { ORS="" }
    { for(i=1;i<=length($0);i++) {
        c=substr($0,i,1)
        if(c=="\\") printf "\\\\\\\\"
        else if(c=="\"" || c=="$" || c=="`") printf "\\\\%s",c
        else if(c=="%") printf "%%" "%%"
        else printf "%s",c
    }}')
icon=$(printf '%s' "$app_dir/_internal/frontend/dist/favicon.svg" | sed 's/\\/\\\\/g')
umask 022
# noclobber closes the race with a newly created existing shortcut.
set -C
{
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            'Name=@NAME@') printf 'Name=%s\n' "$name" ;;
            'Exec=@LAUNCHER@') printf 'Exec=/bin/sh "%s"\n' "$exec_path" ;;
            'Icon=@ICON@') printf 'Icon=%s\n' "$icon" ;;
            *) printf '%s\n' "$line" ;;
        esac
    done < "$app_dir/Keivotos.desktop.in"
} > "$target" || fail "Could not create shortcut: $target. No existing shortcut was replaced." 73
printf 'Installed: %s\nThe application folder must stay at: %s\n' "$target" "$app_dir"
