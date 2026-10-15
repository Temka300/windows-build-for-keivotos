#!/bin/sh
set -eu
entry=$0
if command -v readlink >/dev/null 2>&1; then entry=$(readlink -f -- "$entry"); fi
app_dir=$(CDPATH= cd -- "$(dirname -- "$entry")" && pwd -P)
# Preserve explicit overrides and the application's existing portable.txt rules.
if [ -z "${KEIVOTOS_HOME:-}" ] && [ ! -f "$app_dir/portable.txt" ]; then
    KEIVOTOS_HOME="$app_dir/data"
    export KEIVOTOS_HOME
fi
exec /bin/sh "$app_dir/start-keivotos.sh" "$@"
