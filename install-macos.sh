#!/bin/bash
# Map in a Box — macOS installer helper
#
# Run this script once after downloading to remove the macOS quarantine
# flag and launch the app. After first launch you can open MapInABox.app
# directly from your Applications folder or the extracted zip location.
#
# Usage (in Terminal):
#   bash install-macos.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -d "$SCRIPT_DIR/MapInABox-Education.app" ]; then
    APP_NAME="MapInABox-Education.app"
else
    APP_NAME="MapInABox.app"
fi
APP="$SCRIPT_DIR/$APP_NAME"

if [ ! -d "$APP" ]; then
    echo "Error: MapInABox.app or MapInABox-Education.app not found next to this script."
    echo "Make sure both files are in the same folder."
    exit 1
fi

echo "Removing macOS quarantine flag from $APP_NAME..."
xattr -rd com.apple.quarantine "$APP"

echo "Copying $APP_NAME to /Applications..."
cp -r "$APP" "/Applications/$APP_NAME"

echo "Done. Launching Map in a Box..."
open "/Applications/$APP_NAME"
