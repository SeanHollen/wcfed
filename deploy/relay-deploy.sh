#!/usr/bin/env bash
# Deploy the relay to a public host, behind an existing nginx + TLS.
#
#   RELAY_HOST=root@1.2.3.4 RELAY_SSH_KEY=~/.ssh/id_ed25519 \
#   RELAY_ORGS='orga:tokenA,orgb:tokenB' \
#   RELAY_NGINX_SITE=/etc/nginx/sites-available/yoursite \
#     ./deploy/relay-deploy.sh
#
# Adds, in this order: the code under /opt/wcfed, a systemd unit bound to
# 127.0.0.1:8787, and an nginx `location /wcfed/` on an existing TLS vhost.
#
# It binds to loopback rather than opening a port because the relay token
# travels in a header — it needs TLS, and a vhost that already has a
# certificate is the cheapest way to get it.
#
# TEARDOWN (everything this script does, undone):
#   ssh $RELAY_HOST 'systemctl disable --now wcfed-relay
#                    rm -f /etc/systemd/system/wcfed-relay.service
#                    rm -rf /etc/wcfed /opt/wcfed
#                    systemctl daemon-reload'
#   then remove the `location /wcfed/` block and reload nginx. The script keeps
#   a timestamped .bak-wcfed-* of the vhost next to it.
set -euo pipefail

HOST="${RELAY_HOST:?set RELAY_HOST=user@host}"
KEY="${RELAY_SSH_KEY:-$HOME/.ssh/id_ed25519}"
ORGS="${RELAY_ORGS:?set RELAY_ORGS='orga:tokenA,orgb:tokenB'}"
SITE="${RELAY_NGINX_SITE:-}"
PORT="${RELAY_PORT:-8787}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(ssh -i "$KEY" "$HOST")

echo "==> syncing code to $HOST:/opt/wcfed"
rsync -az --delete --exclude '__pycache__' --exclude '.git' --exclude '*.env' \
  -e "ssh -i $KEY" "$ROOT/" "$HOST:/opt/wcfed/"

echo "==> installing service"
"${SSH[@]}" bash -s <<EOF
set -e
install -d -m 700 /etc/wcfed
printf 'WCFED_RELAY_TOKENS=%s\n' '$ORGS' > /etc/wcfed/relay.env
chmod 600 /etc/wcfed/relay.env

cat > /etc/systemd/system/wcfed-relay.service <<'UNIT'
[Unit]
Description=wcfed federation relay
After=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/wcfed/relay.env
WorkingDirectory=/opt/wcfed
Environment=PYTHONPATH=/opt/wcfed
ExecStart=/usr/bin/python3 -m wcfed.cli relay --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now wcfed-relay
sleep 2
systemctl is-active wcfed-relay
curl -fsS http://127.0.0.1:$PORT/v1/health && echo
EOF

if [ -n "$SITE" ]; then
  echo "==> adding nginx location to $SITE"
  "${SSH[@]}" bash -s <<EOF
set -e
cp "$SITE" "$SITE.bak-wcfed-\$(date +%Y%m%d%H%M%S)"
if grep -q 'location /wcfed/' "$SITE"; then
  echo "location already present, skipping"
else
python3 - "$SITE" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
block = """
    # wcfed federation relay. proxy_read_timeout MUST exceed the gateway's
    # long-poll wait (max 60s), or nginx cuts the connection mid-poll and the
    # gateway sees a stream of 504s instead of an idle queue.
    location /wcfed/ {
        proxy_pass http://127.0.0.1:$PORT/;
        proxy_http_version 1.1;
        proxy_set_header Host \\\$host;
        proxy_set_header X-Real-IP \\\$remote_addr;
        proxy_set_header X-Forwarded-For \\\$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \\\$scheme;
        proxy_read_timeout 90s;
        proxy_send_timeout 90s;
        proxy_buffering off;
        client_max_body_size 1m;
    }
"""
i = s.rstrip().rfind("}")
open(p, "w").write(s[:i] + block + "}\n")
print("inserted")
PY
fi
nginx -t && systemctl reload nginx && echo "nginx reloaded"
EOF
else
  echo "==> RELAY_NGINX_SITE not set; relay is on 127.0.0.1:$PORT only."
  echo "    Add a TLS vhost location yourself, or it is unreachable from outside."
fi

echo
echo "done. Verify from anywhere:  curl https://<your-host>/wcfed/v1/health"
