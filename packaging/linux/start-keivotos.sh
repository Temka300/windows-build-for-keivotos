#!/bin/sh
# The frozen application remains the authority for profile and resource paths.
set -eu
fail() { printf 'Keivotos: %s\n' "$1" >&2; exit "$2"; }
entry=$0
if command -v readlink >/dev/null 2>&1; then entry=$(readlink -f -- "$entry"); fi
app_dir=$(CDPATH= cd -- "$(dirname -- "$entry")" && pwd -P)
cd -- "$app_dir"

check_executable() {
    target=$1
    [ -f "$target" ] || fail "Missing bundled program: $target. Extract the entire application folder." 66
    if command -v findmnt >/dev/null 2>&1; then
        options=$(findmnt --target "$target" --noheadings --output VFS-OPTIONS 2>/dev/null || true)
        case ",$options," in
            *,noexec,*) fail "This drive blocks programs (noexec): $target. Copy the entire application folder to an executable location under your Linux home. chmod alone cannot fix noexec." 126 ;;
        esac
    fi
    [ -x "$target" ] || fail "Execute permission is missing: $target. Allow executing it in Properties, or run chmod u+x on that file. No administrator access is needed for files you own." 126
}

check_executable "$app_dir/Keivotos"
case "${1:-}" in --help|-h|--version) exec "$app_dir/Keivotos" "$@" ;; esac
[ -f "$app_dir/linux-tools.txt" ] || fail 'Missing linux-tools.txt. Extract the complete application folder.' 66
while IFS= read -r tool || [ -n "$tool" ]; do
    case "$tool" in ''|.*|*[!A-Za-z0-9._-]*) fail 'Invalid bundled-tool list. Extract a verified application archive.' 66 ;; esac
    check_executable "$app_dir/$tool"
done < "$app_dir/linux-tools.txt"

if inspection=$("$app_dir/Keivotos" --portable-check 2>&1); then :; else
    printf '%s\n' "$inspection" >&2
    fail 'The application resource check failed. Keep the whole extracted folder together and check the reported missing file or host library.' 70
fi
data_dir=$(printf '%s\n' "$inspection" | sed -n 's/^Writable home: //p')
[ -n "$data_dir" ] || fail 'The application did not report its data directory.' 70
if ! mkdir -p -- "$data_dir" 2>/dev/null; then
    fail "Cannot create the data folder: $data_dir. Choose a writable location owned by your Linux user." 73
fi
if probe=$(mktemp "$data_dir/.keivotos-write-check.XXXXXX" 2>/dev/null); then
    rm -- "$probe"
else
    fail "Cannot write to the data folder: $data_dir. Check ownership, permissions and whether the drive is read-only. Existing files were left in place." 73
fi
if [ "${1:-}" = '--launcher-check' ]; then
    [ "$#" -eq 1 ] || fail '--launcher-check takes no additional arguments.' 64
    printf 'Linux launcher: ready\nApplication: %s\nData: %s\n' "$app_dir" "$data_dir"
    exit 0
fi
printf 'Keivotos data: %s\n' "$data_dir"
exec "$app_dir/Keivotos" "$@"
