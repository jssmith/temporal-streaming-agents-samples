#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="$(cd "$(dirname "$0")" && pwd)/data"
DB_FILE="$DATA_DIR/chinook.sqlite"

if [ -f "$DB_FILE" ]; then
    echo "Database already exists at $DB_FILE"
    exit 1
fi

mkdir -p "$DATA_DIR"

echo "Downloading Chinook SQLite database..."
curl -fSL \
    "https://github.com/lerocha/chinook-database/releases/download/v1.4.5/Chinook_Sqlite.sqlite" \
    -o "$DB_FILE"

echo "Downloaded to $DB_FILE"
