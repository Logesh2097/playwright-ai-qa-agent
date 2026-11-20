#!/usr/bin/env bash
# Creates .env from .env.example and fills in generated secrets.
# Safe to re-run: existing values are kept, only missing secrets are added.
set -euo pipefail

cd "$(dirname "$0")/.."
umask 077

generate() {
  head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "$1"
}

if [[ ! -f .env ]]; then
  sed -e "s|^APP_UID=.*|APP_UID=$(id -u)|" .env.example > .env
  echo "Created .env from .env.example."
fi

# name:length of every secret that is generated rather than chosen
for entry in POSTGRES_PASSWORD:32 APP_DB_PASSWORD:32 JWT_SECRET_KEY:48 RUNNER_TOKEN:48 EGRESS_ADMIN_TOKEN:48 ENGINE_TOKEN:48; do
  name="${entry%%:*}"
  length="${entry##*:}"
  if grep -qE "^${name}=.+" .env; then
    continue
  fi
  value="$(generate "$length")"
  if grep -qE "^${name}=" .env; then
    sed -i "s|^${name}=.*|${name}=${value}|" .env
  else
    printf '%s=%s\n' "$name" "$value" >> .env
  fi
  echo "Generated ${name}."
done

# Credential master key: "<version>:<base64 of 32 random bytes>". Losing it makes every
# stored credential unreadable, so back it up separately from the database.
if ! grep -qE "^CREDENTIAL_MASTER_KEYS=.+" .env; then
  key="1:$(head -c 32 /dev/urandom | base64)"
  if grep -qE "^CREDENTIAL_MASTER_KEYS=" .env; then
    sed -i "s|^CREDENTIAL_MASTER_KEYS=.*|CREDENTIAL_MASTER_KEYS=${key}|" .env
  else
    printf 'CREDENTIAL_MASTER_KEYS=%s\n' "$key" >> .env
  fi
  echo "Generated CREDENTIAL_MASTER_KEYS."
fi

# Settings added after the first release of this template.
grep -qE "^APP_DB_USER=" .env || echo "APP_DB_USER=qa_agent_app" >> .env
