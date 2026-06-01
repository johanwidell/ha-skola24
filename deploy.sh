#!/usr/bin/env bash
# deploy.sh — kopiera integrationen till HAOS via scp
# Användning: ./deploy.sh <haos-ip-eller-hostname>
# Exempel:   ./deploy.sh 192.168.1.100

set -euo pipefail

HAOS_HOST="${1:?Ange HAOS-IP som argument: ./deploy.sh 192.168.1.100}"
REMOTE_PATH="/config/custom_components"

echo "→ Kopierar custom_components/skola24/ till ${HAOS_HOST}:${REMOTE_PATH}/"
scp -r custom_components/skola24 "root@${HAOS_HOST}:${REMOTE_PATH}/"

echo ""
echo "✓ Klart! Nästa steg:"
echo "  1. Gå till HA → Inställningar → System → Starta om"
echo "  2. Gå till Inställningar → Enheter & tjänster → Lägg till integration → Skola24"
echo "  3. Ange:"
echo "       Host:        uppsala.skola24.se"
echo "       Användarnamn: ditt Skola24-login"
echo "       Lösenord:     ditt lösenord"
echo "       Schematyp:    Personnummer (ÅÅMMDD-XXXX)"
