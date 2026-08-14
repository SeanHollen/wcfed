#!/usr/bin/env bash
# Give the relay its OWN hostname, rather than a path on somebody else's vhost.
#
#   RELAY_FQDN=relay.example.com RELAY_HOST=root@1.2.3.4 \
#   RELAY_SSH_KEY=~/.ssh/id_ed25519 ./deploy/relay-subdomain.sh
#
# Prerequisite: an A record for $RELAY_FQDN already points at the host. The
# script checks this first and refuses otherwise, because certbot's HTTP-01
# challenge will fail anyway and a failed issuance counts against Let's
# Encrypt's rate limit (5 per hostname per week).
#
# Assumes the relay service itself is already installed (relay-deploy.sh).
#
# TEARDOWN:
#   ssh $RELAY_HOST 'rm -f /etc/nginx/sites-enabled/wcfed-relay \
#                          /etc/nginx/sites-available/wcfed-relay
#                    nginx -t && systemctl reload nginx
#                    certbot delete --cert-name '"$RELAY_FQDN"
set -euo pipefail

FQDN="${RELAY_FQDN:?set RELAY_FQDN=relay.example.com}"
HOST="${RELAY_HOST:?set RELAY_HOST=user@host}"
KEY="${RELAY_SSH_KEY:-$HOME/.ssh/id_ed25519}"
PORT="${RELAY_PORT:-8787}"
EMAIL="${CERTBOT_EMAIL:-seanahollen@gmail.com}"
SSH=(ssh -i "$KEY" "$HOST")

TARGET_IP="${HOST#*@}"
echo "==> checking DNS for $FQDN"
RESOLVED="$("${SSH[@]}" "dig +short A $FQDN | tail -1" 2>/dev/null || true)"
if [ -z "$RESOLVED" ]; then
  echo "  $FQDN does not resolve yet." >&2
  echo "  Add an A record:  $FQDN -> $TARGET_IP   then re-run." >&2
  exit 1
fi
if [ "$RESOLVED" != "$TARGET_IP" ]; then
  echo "  $FQDN resolves to $RESOLVED, expected $TARGET_IP." >&2
  echo "  Fix the A record (or wait for TTL) and re-run." >&2
  exit 1
fi
echo "  $FQDN -> $RESOLVED, correct"

echo "==> issuing certificate and installing vhost"
"${SSH[@]}" bash -s <<EOF
set -e
command -v certbot >/dev/null || { echo "certbot not installed" >&2; exit 1; }

# Port 80 vhost first: certbot's HTTP-01 challenge needs to be served before a
# certificate exists, so the TLS server block cannot be written yet.
cat > /etc/nginx/sites-available/wcfed-relay <<'CONF'
server {
    listen 80;
    server_name $FQDN;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }
    location / {
        return 301 https://\\\$server_name\\\$request_uri;
    }
}
CONF
ln -sf /etc/nginx/sites-available/wcfed-relay /etc/nginx/sites-enabled/wcfed-relay
nginx -t && systemctl reload nginx

if [ ! -d "/etc/letsencrypt/live/$FQDN" ]; then
  certbot certonly --webroot -w /var/www/html -d "$FQDN" \
    --non-interactive --agree-tos -m "$EMAIL"
else
  echo "certificate already present, reusing"
fi

cat > /etc/nginx/sites-available/wcfed-relay <<'CONF'
server {
    listen 80;
    server_name $FQDN;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }
    location / {
        return 301 https://\\\$server_name\\\$request_uri;
    }
}

server {
    listen 443 ssl;
    server_name $FQDN;

    ssl_certificate     /etc/letsencrypt/live/$FQDN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$FQDN/privkey.pem;

    # The relay is the ONLY thing on this name. No other app to collide with,
    # which is the entire reason for a dedicated hostname.
    location / {
        proxy_pass http://127.0.0.1:$PORT/;
        proxy_http_version 1.1;
        proxy_set_header Host \\\$host;
        proxy_set_header X-Real-IP \\\$remote_addr;
        proxy_set_header X-Forwarded-For \\\$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \\\$scheme;

        # MUST exceed the gateway's long-poll wait (max 60s), or nginx cuts
        # polls mid-flight and every gateway sees a stream of 504s instead of
        # an idle queue.
        proxy_read_timeout 90s;
        proxy_send_timeout 90s;
        proxy_buffering off;
        client_max_body_size 1m;
    }
}
CONF
nginx -t && systemctl reload nginx
echo "vhost installed"
EOF

echo "==> verifying"
sleep 2
curl -fsS --max-time 20 "https://$FQDN/v1/health" && echo
echo
echo "Relay is now at https://$FQDN"
echo "Update WCFED_RELAY_URL on every gateway (yours and every peer's)."
