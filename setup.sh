#!/usr/bin/env bash
set -e

VENV_DIR="venv"

echo "Creating virtual environment in ./$VENV_DIR ..."
python -m venv "$VENV_DIR"

echo "Installing dependencies..."
"$VENV_DIR/Scripts/pip" install -r requirements.txt

echo ""
echo "Setup complete. To run:"
echo "  source venv/Scripts/activate"
echo "  python gmail_helper.py"
