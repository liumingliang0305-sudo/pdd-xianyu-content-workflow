#!/usr/bin/env bash
set -euo pipefail
# Keep this process running while using http://localhost:15678/.
exec ssh -N -T \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:15678:127.0.0.1:5678 \
  huawei-cloud
