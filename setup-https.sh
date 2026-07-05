#!/bin/bash
# Usage: ./setup-https.sh options.mamoscrypto.com
# Run AFTER DNS A record points to this VPS (138.68.80.156)

set -e

DOMAIN="${1:-options.mamoscrypto.com}"
PROJECT_DIR="/root/telegram-claude-bot/dashboard_options"

echo "=== HTTPS Setup for $DOMAIN ==="

# 1. Verify DNS points here
RESOLVED_IP=$(dig +short "$DOMAIN" | tail -1)
VPS_IP=$(curl -s ifconfig.me)
if [ "$RESOLVED_IP" != "$VPS_IP" ]; then
    echo "ERROR: DNS not ready. $DOMAIN resolves to $RESOLVED_IP, this VPS is $VPS_IP"
    echo "Update DNS first, then re-run."
    exit 1
fi
echo "DNS OK: $DOMAIN -> $RESOLVED_IP"

# 2. Install certbot if missing
if ! command -v certbot &>/dev/null; then
    apt-get update -qq && apt-get install -y certbot
fi

# 3. Stop nginx to free port 80 for certbot standalone
cd "$PROJECT_DIR"
docker compose stop nginx

# 4. Get certificate
certbot certonly --standalone -d "$DOMAIN" \
    --non-interactive --agree-tos --email crypto.mamos@gmail.com \
    --no-eff-email

# 5. Activate SSL nginx config
sed "s/DOMAIN_PLACEHOLDER/$DOMAIN/g" "$PROJECT_DIR/nginx-ssl.conf" > "$PROJECT_DIR/nginx.conf"

# 6. Switch to SSL docker-compose
cp "$PROJECT_DIR/docker-compose.yml" "$PROJECT_DIR/docker-compose.yml.backup"
cp "$PROJECT_DIR/docker-compose-ssl.yml" "$PROJECT_DIR/docker-compose.yml"

# 7. Restart with SSL
docker compose up -d

# 8. Wait and test
sleep 5
echo ""
echo "=== TEST RESULTS ==="
curl -s -o /dev/null -w "HTTP  $DOMAIN/              → %{http_code}\n" "http://$DOMAIN/"
curl -sk -o /dev/null -w "HTTPS $DOMAIN/              → %{http_code}\n" "https://$DOMAIN/"
curl -sk -o /dev/null -w "HTTPS $DOMAIN/api/dashboard → %{http_code}\n" "https://$DOMAIN/api/dashboard"
curl -sk -o /dev/null -w "HTTPS $DOMAIN/api/mopi_free → %{http_code}\n" "https://$DOMAIN/api/mopi_free"
curl -sk -o /dev/null -w "HTTPS $DOMAIN/api/gex_by_strike → %{http_code}\n" "https://$DOMAIN/api/gex_by_strike"

echo ""
echo "=== SSL CERT INFO ==="
echo | openssl s_client -connect "$DOMAIN:443" -servername "$DOMAIN" 2>/dev/null \
    | openssl x509 -noout -subject -dates 2>/dev/null

echo ""
echo "=== DONE. Dashboard: https://$DOMAIN ==="
echo "Rollback: cp nginx.conf.backup nginx.conf && cp docker-compose.yml.backup docker-compose.yml && docker compose up -d"
