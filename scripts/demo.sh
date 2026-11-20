#!/usr/bin/env sh
# Creates a demo login, project, tests and one finished run on a development stack.
# The login's password is generated once and kept in .env (which git ignores).
set -eu
cd "$(dirname "$0")/.."

if ! grep -q '^DEMO_PASSWORD=' .env; then
    {
        echo ""
        echo "# Login created by make demo (development only)"
        echo "DEMO_EMAIL=demo@example.com"
        echo "DEMO_PASSWORD=$(openssl rand -hex 12)"
    } >> .env
fi

DEMO_EMAIL=$(grep '^DEMO_EMAIL=' .env | cut -d= -f2-)
DEMO_PASSWORD=$(grep '^DEMO_PASSWORD=' .env | cut -d= -f2-)
PORT=$(grep '^FRONTEND_HOST_PORT=' .env | cut -d= -f2-)
export DEMO_EMAIL DEMO_PASSWORD

docker compose --profile demo up -d demo-app
docker compose exec -T -e DEMO_EMAIL -e DEMO_PASSWORD api python - < scripts/seed_demo.py

echo ""
echo "Open      http://localhost:${PORT:-5173}"
echo "Email     $DEMO_EMAIL"
echo "Password  the DEMO_PASSWORD line in .env"
