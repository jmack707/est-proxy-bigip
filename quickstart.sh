#!/usr/bin/env bash
# Drives the deploy fast path (docs/deploy.md) end-to-end from a single
# config file: stands up dev-mode OpenBao, configures + starts the EST shim,
# deploys the BIG-IP objects, and issues + installs the VS's own bootstrap
# certificate. Directory setup (docs/operations/ad-setup.md) and testing
# with estclient stay manual.
#
# Usage:
#   cp deploy.env.example deploy.env   # fill it in
#   ./quickstart.sh
#
# Re-running is safe: OpenBao bootstrap and the BIG-IP deploy are both
# idempotent (existing objects are left alone / reported, not duplicated).
# The bootstrap cert step re-issues a fresh cert each run, which is fine.
set -euo pipefail
cd "$(dirname "$0")"

[ -f deploy.env ] || { echo "ERROR: deploy.env not found. Copy deploy.env.example to deploy.env and fill it in first." >&2; exit 1; }
# shellcheck disable=SC1091
source deploy.env

# Rootless Podman containers die when the session that started them ends,
# unless the user has lingering enabled -- confirmed empirically, see
# docs/operations/troubleshooting.md ("rootless Podman containers die with
# the session"). Only relevant for Podman (Docker
# doesn't have this issue); check + warn rather than silently leaving a
# stack that disappears the moment this SSH session closes.
if command -v podman >/dev/null 2>&1 && ! (docker compose version 2>&1 | grep -qi "docker compose version"); then
  LINGER="$(loginctl show-user "$(whoami)" --property=Linger 2>/dev/null | cut -d= -f2)"
  if [ "$LINGER" = "no" ]; then
    echo "WARNING: lingering is not enabled for $(whoami) -- containers started by this" >&2
    echo "         script may be killed when this session ends. Fix with:" >&2
    echo "           sudo loginctl enable-linger $(whoami)" >&2
  fi
fi

for v in DOMAIN BACKEND_HOST BIGIP_HOST BIGIP_USER BIGIP_PASS VS_DESTINATION VS_VLAN VS_HOSTNAME; do
  [ -n "${!v:-}" ] || { echo "ERROR: $v is not set in deploy.env" >&2; exit 1; }
done

# The OpenBao role created below only allows issuing certs for DOMAIN itself
# or a subdomain of it (allow_subdomains=true) -- VS_HOSTNAME must satisfy
# that or step 6 fails with an opaque "common name ... not allowed by this
# role" error from OpenBao. Catch it here instead, with a clear fix.
if [ "$VS_HOSTNAME" != "$DOMAIN" ] && [[ "$VS_HOSTNAME" != *".$DOMAIN" ]]; then
  echo "ERROR: VS_HOSTNAME ('$VS_HOSTNAME') must equal DOMAIN ('$DOMAIN') or end with '.$DOMAIN'." >&2
  echo "       e.g. DOMAIN=f5lab.local -> VS_HOSTNAME=est.f5lab.local" >&2
  exit 1
fi

ROLE="$(echo "$DOMAIN" | tr '.' '-')"
OPENBAO_LOCAL="http://localhost:8200"
SHIM_PORT=8085  # fixed -- matches docker-compose.yml's port mapping and est-shim.env.example's default

wait_for() {
  local url="$1" tries="${2:-15}"
  for _ in $(seq 1 "$tries"); do
    curl -sf "$url" >/dev/null 2>&1 && return 0
    sleep 2
  done
  echo "ERROR: timed out waiting for $url" >&2
  exit 1
}

echo "==> [1/6] Starting OpenBao (dev mode)"
docker compose up -d openbao
wait_for "$OPENBAO_LOCAL/v1/sys/health"

echo "==> [2/6] Bootstrapping OpenBao PKI + AppRole"
BOOTSTRAP_OUT="$(BAO_ADDR="$OPENBAO_LOCAL" ./bootstrap-openbao-dev.sh "$DOMAIN" "$ROLE")"
echo "$BOOTSTRAP_OUT"
BAO_ROLE_ID="$(echo "$BOOTSTRAP_OUT" | grep '^BAO_ROLE_ID=' | cut -d= -f2)"
BAO_SECRET_ID="$(echo "$BOOTSTRAP_OUT" | grep '^BAO_SECRET_ID=' | cut -d= -f2)"
[ -n "$BAO_ROLE_ID" ] && [ -n "$BAO_SECRET_ID" ] || { echo "ERROR: failed to parse AppRole creds from bootstrap output" >&2; exit 1; }

echo "==> [3/6] Writing est-shim.env"
# NOTE: deliberately not using bash's ${VAR:-default} shorthand for
# LDAP_BIND_DN_TEMPLATE -- when the default itself contains a literal '}'
# (e.g. "{username}"), bash's parser treats the FIRST '}' as closing the
# expansion and leaves the second '}' as a stray literal character in the
# output. Confirmed empirically; explicit defaulting below sidesteps it.
: "${LDAP_ENABLED:=false}"
: "${LDAP_URI:=ldaps://127.0.0.1:636}"
if [ -z "${LDAP_BIND_DN_TEMPLATE:-}" ]; then LDAP_BIND_DN_TEMPLATE='{username}'; fi
: "${LDAP_START_TLS:=false}"
: "${LDAP_ENFORCE_CN_MATCH:=true}"
# est-shim.env holds BAO_SECRET_ID, a credential that can sign certificates.
# Create it unreadable to other users before writing anything into it.
( umask 077; : > est-shim.env )
cat > est-shim.env << EOF
BAO_ADDR=http://openbao:8200
BAO_ROLE_ID=$BAO_ROLE_ID
BAO_SECRET_ID=$BAO_SECRET_ID
PKI_MOUNT=pki_int
PKI_ROLE=$ROLE
DOMAIN=$DOMAIN
LISTEN_PORT=$SHIM_PORT
LDAP_ENABLED=$LDAP_ENABLED
LDAP_URI=$LDAP_URI
LDAP_BIND_DN_TEMPLATE=$LDAP_BIND_DN_TEMPLATE
LDAP_START_TLS=$LDAP_START_TLS
LDAP_REQUIRE_OPS=simpleenroll
LDAP_ENFORCE_CN_MATCH=$LDAP_ENFORCE_CN_MATCH
EOF
chmod 600 est-shim.env   # unconditional: a pre-existing file keeps its old mode through truncation

echo "==> [4/6] Starting the EST shim"
docker compose up -d --build est-shim
wait_for "http://localhost:${SHIM_PORT}/.well-known/est/cacerts"

echo "==> [5/6] Deploying BIG-IP objects"
python3 deploy_bigip.py "$BIGIP_HOST" "$BIGIP_USER" "$BIGIP_PASS" est_proxy.irule.tcl \
  --pool-member "${BACKEND_HOST}:${SHIM_PORT}" --vs-destination "$VS_DESTINATION" --vs-vlan "$VS_VLAN"

echo "==> [6/6] Issuing + installing the VS's own bootstrap certificate"
# Direct from OpenBao, not via EST -- a VS can't use EST through itself for
# its own first certificate (see docs/deploy.md Part 2).
TMP_CERT_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_CERT_DIR"' EXIT
curl -sf -X POST -H "X-Vault-Token: root" \
  -d "{\"common_name\":\"$VS_HOSTNAME\"}" \
  "$OPENBAO_LOCAL/v1/pki_int/issue/$ROLE" \
  | python3 -c "import sys,json; d=json.load(sys.stdin)['data']; open('$TMP_CERT_DIR/vs.crt','w').write(d['certificate']); open('$TMP_CERT_DIR/vs.key','w').write(d['private_key'])"
python3 install-cert-bigip.py --bigip-host "$BIGIP_HOST" --bigip-user "$BIGIP_USER" --bigip-pass "$BIGIP_PASS" \
  --cert-name est-proxy-vs-cert --cert-file "$TMP_CERT_DIR/vs.crt" --key-file "$TMP_CERT_DIR/vs.key" \
  --attach-profile est-clientssl

curl -sf "$OPENBAO_LOCAL/v1/pki_int/ca_chain" -o ca-chain.pem

cat << EOF

==> Done.

  BIG-IP VS:     https://$VS_DESTINATION (EST at /.well-known/est/...)
  VS hostname:   $VS_HOSTNAME (make sure it resolves to ${VS_DESTINATION%%:*} wherever you test from)
  CA chain:      $(pwd)/ca-chain.pem

Test it (see docs/deploy.md "Verification" for the full set, including the negative cases):

  export EST_OPENSSL_CACERT=$(pwd)/ca-chain.pem
  estclient -g -s $VS_HOSTNAME -p ${VS_DESTINATION##*:} -o /tmp/est-out
EOF
