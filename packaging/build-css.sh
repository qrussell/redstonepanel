#!/bin/bash
# Regenerates static/css/tailwind.css from the classes used in static/**/*.html and static/js/*.js.
# Run it after adding or changing Tailwind classes in the UI, and commit the result.
# Needs Node.js (npx); the Tailwind CLI version is pinned to match the v3 classes the UI uses.
set -euo pipefail
cd "$(dirname "$0")/.."
npx --yes tailwindcss@3.4.17 \
    -c packaging/tailwind/tailwind.config.js \
    -i packaging/tailwind/input.css \
    -o static/css/tailwind.css --minify
