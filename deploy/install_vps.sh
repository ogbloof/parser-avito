#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/avito-bot"

sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip

# Для Selenium (Chrome)
sudo apt-get install -y wget gnupg ca-certificates
if ! command -v google-chrome >/dev/null 2>&1; then
  wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | sudo gpg --dearmor -o /usr/share/keyrings/google-linux-signing-keyring.gpg
  echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux-signing-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" | sudo tee /etc/apt/sources.list.d/google-chrome.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y google-chrome-stable
fi

sudo mkdir -p "$APP_DIR"
sudo chown -R "$USER":"$USER" "$APP_DIR"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -U pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo ""
echo "Теперь положи проект в $APP_DIR и создай $APP_DIR/.env (см. .env.example)."
echo "Дальше установи сервис:"
echo "  sudo cp $APP_DIR/deploy/avito-bot.service /etc/systemd/system/avito-bot.service"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now avito-bot"

