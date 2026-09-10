#!/usr/bin/env bash
# מריץ את ה-connector (Baileys). דורש npm install מראש בתיקיית connector-whatsapp.
set -euo pipefail
cd "$(dirname "$0")/../connector-whatsapp"
npm start
