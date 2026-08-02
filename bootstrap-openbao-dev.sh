#!/usr/bin/env bash
# Stand up a throwaway OpenBao PKI (dev mode) and an AppRole scoped for
# est_shim.py -- for environments without an existing Vault/OpenBao instance
# (e.g. F5 UDF). NOT for production: dev mode is in-memory, single unseal
# key, root token known ahead of time. Every command here is validated
# against a real OpenBao 2.2.0 instance, not guessed from docs.
#
# Usage: BAO_ADDR=http://127.0.0.1:8210 ./bootstrap-openbao-dev.sh [domain] [role]
#   domain defaults to example.com, role defaults to <domain-with-dashes>
set -euo pipefail

BAO_ADDR="${BAO_ADDR:-http://127.0.0.1:8210}"
BAO_TOKEN="${BAO_TOKEN:-root}"
DOMAIN="${1:-example.com}"
ROLE="${2:-$(echo "$DOMAIN" | tr '.' '-')}"

api() { curl -sf -H "X-Vault-Token: $BAO_TOKEN" -H "Content-Type: application/json" "$@"; }

echo "==> Mounting root CA (pki)"
api -X POST -d '{"type":"pki"}' "$BAO_ADDR/v1/sys/mounts/pki" >/dev/null || true
api -X POST -d '{"max_lease_ttl":"87600h"}' "$BAO_ADDR/v1/sys/mounts/pki/tune" >/dev/null

echo "==> Generating root CA"
api -X POST -d "{\"common_name\":\"$DOMAIN Root CA\",\"ttl\":\"87600h\"}" \
  "$BAO_ADDR/v1/pki/root/generate/internal" >/dev/null

echo "==> Mounting intermediate CA (pki_int)"
api -X POST -d '{"type":"pki"}' "$BAO_ADDR/v1/sys/mounts/pki_int" >/dev/null || true
api -X POST -d '{"max_lease_ttl":"43800h"}' "$BAO_ADDR/v1/sys/mounts/pki_int/tune" >/dev/null

echo "==> Generating + signing intermediate CA"
CSR=$(api -X POST -d "{\"common_name\":\"$DOMAIN Intermediate CA\"}" \
  "$BAO_ADDR/v1/pki_int/intermediate/generate/internal" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['csr'])")
CERT=$(python3 -c "
import json,sys
print(json.dumps({'csr': sys.argv[1], 'format': 'pem_bundle', 'ttl': '43800h'}))
" "$CSR" | api -X POST -d @- "$BAO_ADDR/v1/pki/root/sign-intermediate" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['certificate'])")
python3 -c "
import json,sys
print(json.dumps({'certificate': sys.argv[1]}))
" "$CERT" | api -X POST -d @- "$BAO_ADDR/v1/pki_int/intermediate/set-signed" >/dev/null

echo "==> Creating signing role '$ROLE' (allowed_domains=$DOMAIN)"
# use_csr_sans=false: without it the CA copies whatever SANs a CSR asks for into
# the issued certificate. Since TLS peers validate against SANs, that lets an
# authenticated client obtain a certificate naming somebody else, defeating
# LDAP_ENFORCE_CN_MATCH. The shim rejects such CSRs too; this is the second layer.
api -X POST -d "{\"allowed_domains\":\"$DOMAIN\",\"allow_subdomains\":true,\"max_ttl\":\"720h\",\"use_csr_sans\":false}" \
  "$BAO_ADDR/v1/pki_int/roles/$ROLE" >/dev/null

echo "==> Enabling AppRole auth + est-shim policy + role"
api -X POST -d '{"type":"approle"}' "$BAO_ADDR/v1/sys/auth/approle" >/dev/null || true
api -X PUT -d "{\"policy\":\"path \\\"pki_int/issue/$ROLE\\\" { capabilities = [\\\"update\\\"] }\npath \\\"pki_int/sign/$ROLE\\\" { capabilities = [\\\"update\\\"] }\n\"}" \
  "$BAO_ADDR/v1/sys/policies/acl/est-shim" >/dev/null
api -X POST -d '{"token_policies":"est-shim","token_ttl":"30m","token_max_ttl":"2h"}' \
  "$BAO_ADDR/v1/auth/approle/role/est-shim" >/dev/null

ROLE_ID=$(api "$BAO_ADDR/v1/auth/approle/role/est-shim/role-id" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['role_id'])")
SECRET_ID=$(api -X POST "$BAO_ADDR/v1/auth/approle/role/est-shim/secret-id" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['secret_id'])")

echo
echo "==> Done. Add these to est-shim.env:"
echo "BAO_ADDR=$BAO_ADDR"
echo "BAO_ROLE_ID=$ROLE_ID"
echo "BAO_SECRET_ID=$SECRET_ID"
echo "PKI_MOUNT=pki_int"
echo "PKI_ROLE=$ROLE"
echo "DOMAIN=$DOMAIN"
echo
echo "==> CA chain (for EST_OPENSSL_CACERT / VS cert issuance):"
echo "    curl -s $BAO_ADDR/v1/pki_int/ca_chain > ca-chain.pem"
