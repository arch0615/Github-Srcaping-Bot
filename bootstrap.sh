#!/usr/bin/env bash
# One-time setup. Installs system deps (needs sudo), Python deps, and Postgres.
set -e

echo ">> Installing python venv/pip + Docker (requires sudo)..."
sudo apt update
sudo apt install -y python3-venv python3-pip docker.io docker-compose-plugin
sudo usermod -aG docker "$USER" || true   # may need re-login to take effect

echo ">> Creating virtualenv + installing Python deps..."
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

echo ">> Starting PostgreSQL..."
sudo docker compose up -d

echo
echo ">> Done. Next:"
echo "   1. cp .env.example .env   then paste your GITHUB_TOKEN"
echo "   2. source .venv/bin/activate && python github_scraper.py"
