#!/bin/sh
# Simple entrypoint script to set runtime UID/GID

# Exit on error
set -e

# Define default UID/GID and username/groupname (should match build ARGs)
DEFAULT_UID=10001
DEFAULT_GID=10001
USERNAME=exporter
GROUPNAME=exporter

# Use environment variables PUID/PGID if set, otherwise use defaults
CURRENT_UID=${PUID:-$DEFAULT_UID}
CURRENT_GID=${PGID:-$DEFAULT_GID}

# Check if UID/GID needs changing
EXISTING_UID=$(id -u "$USERNAME")
EXISTING_GID=$(id -g "$GROUPNAME")

echo "Starting with UID: $CURRENT_UID, GID: $CURRENT_GID"

if [ "$CURRENT_GID" != "$EXISTING_GID" ]; then
  echo "Changing group $GROUPNAME GID from $EXISTING_GID to $CURRENT_GID"
  groupmod -o -g "$CURRENT_GID" "$GROUPNAME"
fi

if [ "$CURRENT_UID" != "$EXISTING_UID" ]; then
  echo "Changing user $USERNAME UID from $EXISTING_UID to $CURRENT_UID"
  usermod -o -u "$CURRENT_UID" "$USERNAME"
fi

# Ensure ownership of the app directory
# This might be needed if volumes are mounted with different ownership
echo "Updating /app ownership ..."
chown -R "$CURRENT_UID":"$CURRENT_GID" /app

# Drop privileges and execute the main command (passed as arguments to this script)
# "$@" will be replaced by the CMD from the Dockerfile (e.g., "python" "plex_exporter.py")
echo "Executing command as user $USERNAME: $@"
exec gosu "$USERNAME" "$@"

