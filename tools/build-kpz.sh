#!/usr/bin/env bash
# Package the Koha plugin as a .kpz (a zip of the Koha/ tree).
set -euo pipefail
cd "$(dirname "$0")/../koha-plugin"
VERSION=$(grep -oE 'our \$VERSION = "[^"]+"' Koha/Plugin/Com/BookRS/Widget.pm | grep -oE '[0-9][^"]*')
OUT="../dist/koha-plugin-bookrs-widget-v${VERSION}.kpz"
mkdir -p ../dist
rm -f "$OUT"
zip -qr "$OUT" Koha
echo "built $OUT"
