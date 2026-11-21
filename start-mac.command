#!/bin/bash
# B.E.R.B for Mac: double-click this to set up (first time) and open B.E.R.B.
# If macOS says it can't be opened, right-click it, choose Open, then Open again.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  echo "B.E.R.B needs Python 3.10 or newer. Opening the download page..."
  open "https://www.python.org/downloads/"
  read -r -p "Install it, then double-click start-mac.command again. Press Enter to close."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Setting up B.E.R.B. The first time takes a minute or two..."
  python3 -m venv .venv || { read -r -p "Setup failed. Press Enter to close."; exit 1; }
fi
echo "Checking that everything B.E.R.B needs is installed..."
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt \
  || { read -r -p "Setup failed. Press Enter to close."; exit 1; }

echo "Opening B.E.R.B..."
.venv/bin/python -m taskrunner.cli open || { read -r -p "Couldn't start B.E.R.B. Press Enter to close."; exit 1; }
echo
echo "All set! B.E.R.B runs quietly in the background; use Turn off in the app to stop it."
echo "Next time, just double-click start-mac.command again. You can close this window."
