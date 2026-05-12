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
APP="$SCRIPT_DIR/MapInABox.app"

if [ ! -d "$APP" ]; then
    echo "Error: MapInABox.app not found next to this script."
    echo "Make sure both files are in the same folder."
    exit 1
fi

echo "Removing macOS quarantine flag from MapInABox.app..."
xattr -rd com.apple.quarantine "$APP"

echo "Copying MapInABox.app to /Applications..."
cp -r "$APP" /Applications/MapInABox.app

echo "Done. Launching Map in a Box..."
open /Applications/MapInABox.app
