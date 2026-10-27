#!/usr/bin/env bash
# Install in Hisho (src/hisho/companion.py runs this on the Core machine): Forgejo in rootless Docker with
# PostgreSQL, on the tailnet at https://<core>:3443 (git over ssh :2222). Hisho's own install does the work,
# with the pinned versions its self-update keeps current.
set -euo pipefail
HISHO_REPO="${HISHO_REPO:-$HOME/code/hisho}"
exec bash "$HISHO_REPO/deploy/forgejo/install.sh"
