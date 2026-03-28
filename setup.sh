#!/bin/bash
# Hermes setup — run once to get started

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Setting up Hermes..."

if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not found. Install from https://python.org"
    exit 1
fi

PYTHON=$(command -v python3)
echo "Python: $($PYTHON --version)"

if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv venv
fi

source venv/bin/activate
echo "Installing dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "Dependencies installed."
echo ""

# Check what's missing from .env
MISSING=""
if ! grep -q "CANVAS_TOKEN=." .env 2>/dev/null; then MISSING="$MISSING CANVAS_TOKEN"; fi
if ! grep -q "GEMINI_API_KEY=." .env 2>/dev/null; then MISSING="$MISSING GEMINI_API_KEY"; fi

if [ -n "$MISSING" ]; then
    echo "---------------------------------------------------"
    echo "ACTION REQUIRED: Fill in .env before starting"
    echo ""
    for key in $MISSING; do
        case $key in
            CANVAS_TOKEN)
                echo "CANVAS_TOKEN:"
                echo "  1. Go to https://osu.instructure.com/profile/settings"
                echo "  2. Scroll to 'Approved Integrations' at the bottom"
                echo "  3. Click 'New Access Token', name it 'Hermes', no expiry"
                echo "  4. Copy the token and paste after CANVAS_TOKEN= in .env"
                echo ""
                ;;
            GEMINI_API_KEY)
                echo "GEMINI_API_KEY (free, no credit card):"
                echo "  1. Go to https://aistudio.google.com/app/apikey"
                echo "  2. Sign in with your Google account"
                echo "  3. Click 'Create API Key'"
                echo "  4. Paste it after GEMINI_API_KEY= in .env"
                echo ""
                ;;
        esac
    done
    echo "Optional: add Twilio credentials for SMS/Apple Watch alerts"
    echo "  Sign up at https://www.twilio.com/try-twilio (free ~\$15 credit)"
    echo "  Hermes works fine without it — use the web dashboard instead."
    echo "---------------------------------------------------"
else
    echo "Credentials configured."

    # Offer to install as background service
    read -p "Install Hermes as a background service (auto-starts on login)? (y/n): " install_launchd
    if [ "$install_launchd" = "y" ]; then
        PYTHON_PATH="$SCRIPT_DIR/venv/bin/python"
        PLIST_DEST="$HOME/Library/LaunchAgents/com.hermes.daemon.plist"

        cat > "$PLIST_DEST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hermes.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_PATH</string>
        <string>$SCRIPT_DIR/hermes.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/logs/hermes.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/logs/hermes.stderr.log</string>
</dict>
</plist>
PLIST

        launchctl load "$PLIST_DEST"
        echo ""
        echo "Hermes installed as background service."
        echo "  Dashboard: http://localhost:5000"
        echo "  Stop:   launchctl unload $PLIST_DEST"
        echo "  Status: launchctl list | grep hermes"
    else
        echo ""
        echo "To run Hermes:"
        echo "  cd ~/hermes"
        echo "  source venv/bin/activate"
        echo "  python hermes.py"
        echo ""
        echo "Then open: http://localhost:5000"
    fi
fi
