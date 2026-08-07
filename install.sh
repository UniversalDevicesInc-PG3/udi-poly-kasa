#!/usr/bin/env bash
# PG3 Node Server install: nested python-kasa (TPAP) + pip deps.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PLUGIN_DIR"

# Nested python-kasa with TPAP + FreeBSD gmpy2.mpz fix (jimboca fork).
# Excluded from the store zip; cloned here so import uses plugin_dir/kasa.
PYTHON_KASA_REPO="${PYTHON_KASA_REPO:-https://github.com/jimboca/python-kasa.git}"
PYTHON_KASA_BRANCH="${PYTHON_KASA_BRANCH:-tpap-gmpy2-mpz-fix}"

install_nested_python_kasa() {
  if ! command -v git >/dev/null 2>&1; then
    for candidate in /usr/local/bin/git /usr/bin/git; do
      if [ -x "$candidate" ]; then
        PATH="$(dirname "$candidate"):$PATH"
        export PATH
        break
      fi
    done
  fi
  if ! command -v git >/dev/null 2>&1; then
    echo "WARNING: git not found; cannot install nested python-kasa ($PYTHON_KASA_BRANCH)."
    echo "         TPAP devices (e.g. KP125M) will not work until git is available."
    return 1
  fi

  if [ -d python-kasa/.git ]; then
    echo "Updating nested python-kasa ($PYTHON_KASA_BRANCH)..."
    git -C python-kasa remote set-url origin "$PYTHON_KASA_REPO" 2>/dev/null || true
    git -C python-kasa fetch --depth 1 origin "$PYTHON_KASA_BRANCH"
    git -C python-kasa checkout -B "$PYTHON_KASA_BRANCH" "FETCH_HEAD"
  else
    echo "Cloning nested python-kasa ($PYTHON_KASA_BRANCH)..."
    rm -rf python-kasa
    git clone --depth 1 --branch "$PYTHON_KASA_BRANCH" --single-branch \
      "$PYTHON_KASA_REPO" python-kasa
  fi

  # Import path: plugin_dir/kasa -> python-kasa/kasa
  rm -rf kasa
  ln -sfn python-kasa/kasa kasa

  HEAD="$(git -C python-kasa rev-parse HEAD 2>/dev/null || true)"
  python3 - "$PYTHON_KASA_REPO" "$PYTHON_KASA_BRANCH" "$HEAD" <<'PY'
import json, sys
from datetime import datetime, timezone
repo, branch, head = sys.argv[1:4]
payload = {
    "enabled": True,
    "repo": repo,
    "branch": branch,
    "head": head or None,
    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
with open(".dev_python_kasa.json", "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2, sort_keys=True)
    fh.write("\n")
print("Wrote .dev_python_kasa.json (enabled, %s @ %s)" % (branch, (head or "?")[:12]))
PY
  return 0
}

install_nested_python_kasa || true

if [ $# -gt 0 ]; then
  echo "Skipping pip3 install, must be a travis run?"
else
  pip3 install --upgrade pip
  # ecdsa + passlib required by TPAP transport. On FreeBSD, if pip has no
  # wheel, prefer: pkg install py311-ecdsa py311-passlib
  pip3 install -r requirements.txt --user --no-warn-script-location --upgrade
fi
