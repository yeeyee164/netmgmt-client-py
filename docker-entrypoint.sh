#!/usr/bin/env sh
set -e

# Default to showing help if no arguments provided
if [ $# -eq 0 ]; then
    exec python /app/client_main.py --help
fi

# If the first argument begins with a dash (e.g. -c, -h, --help)
if [ "${1#-}" != "$1" ]; then
    exec python /app/client_main.py "$@"
fi

# If the first argument is a recognized protocol subcommand
if [ "$1" = "gnmi" ] || [ "$1" = "netconf" ]; then
    exec python /app/client_main.py "$@"
fi

# Otherwise, execute user-provided command (e.g. pytest, python, bash, sh)
exec "$@"

