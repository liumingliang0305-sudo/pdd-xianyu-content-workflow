#!/usr/bin/env bash
# Run on the cloud host from the deployed directory. Briefly stops n8n.
set -euo pipefail
umask 077
cd -- "$(dirname -- "$0")"
test -s .env
test -f data/database.sqlite
mkdir -p backups
backup_path="backups/n8n-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
docker compose stop n8n
trap 'docker compose start n8n >&2' EXIT
tar -czf "$backup_path" .env compose.yaml image-manifest.json data
chmod 600 "$backup_path"
printf '%s\n' "$backup_path"
