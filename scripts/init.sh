#!/usr/bin/env bash
# Creates the local, git-ignored files a fresh checkout needs. Never overwrites.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f inventory.ini ]; then
  cp inventory.example.ini inventory.ini
  echo "created inventory.ini: put your hosts in it"
fi

if [ ! -f vars/secrets.yml ]; then
  # hex: the DB password goes into a DSN unescaped
  sed -e "s/^jobmon_db_password: \"\"/jobmon_db_password: \"$(openssl rand -hex 24)\"/" \
      -e "s/^jobmon_grafana_password: \"\"/jobmon_grafana_password: \"$(openssl rand -hex 12)\"/" \
      vars/secrets.example.yml > vars/secrets.yml
  chmod 600 vars/secrets.yml
  echo "created vars/secrets.yml with random jobmon passwords: add the Tailscale keys"
fi

if [ ! -f app/.env ]; then
  sed -e "s/^DB_PASSWORD=.*/DB_PASSWORD=$(openssl rand -hex 24)/" \
      -e "s/^GRAFANA_PASSWORD=.*/GRAFANA_PASSWORD=$(openssl rand -hex 12)/" \
      app/.env.example > app/.env
  chmod 600 app/.env
  echo "created app/.env (only for running jobmon with docker compose)"
fi
