#!/usr/bin/env bash
#
# Issue the lab directory's LDAPS certificate.
#
# Prefers the lab CA already standing in the compose stack, so the fixture's
# certificate validates against the same ca-chain.pem everything else uses.
# Falls back to self-signed when the CA is unreachable, and says so — a
# self-signed leaf will not satisfy a verifying LDAP client.
#
# Lab-only. Uses the dev-mode root token by default (ADR-0004).

set -euo pipefail

BAO_ADDR="${BAO_ADDR:-http://127.0.0.1:8200}"
BAO_TOKEN="${BAO_TOKEN:-root}"
PKI_MOUNT="${PKI_MOUNT:-pki_int}"
PKI_ROLE="${PKI_ROLE:-example-dot-com}"
LDAP_CN="${LDAP_CN:-lab-ldap.example.com}"

out_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
crt="${out_dir}/ldap.crt"
key="${out_dir}/ldap.key"

issue_from_ca() {
  local body response
  # Only names inside the role's allowed_domains. A bare `lab-ldap` or
  # `localhost` SAN is refused by a normally-scoped role, so the fixture is
  # reached by its FQDN — see the network alias in docker-compose.lab-ldap.yml.
  body="$(printf '{"common_name":"%s","ip_sans":"127.0.0.1","ttl":"720h"}' "${LDAP_CN}")"
  response="$(curl -s -X POST \
    -H "X-Vault-Token: ${BAO_TOKEN}" \
    -d "${body}" \
    "${BAO_ADDR}/v1/${PKI_MOUNT}/issue/${PKI_ROLE}" 2>/dev/null)" || return 1

  if [ -z "$response" ] || printf '%s' "$response" | grep -q '"errors"'; then
    [ -n "$response" ] && echo "    CA refused: $response" >&2
    return 1
  fi

  EST_LDAP_RESPONSE="$response" python3 -c '
import json, os, sys
crt_path, key_path = sys.argv[1], sys.argv[2]
data = json.loads(os.environ["EST_LDAP_RESPONSE"])["data"]
chain = [data["certificate"]] + list(data.get("ca_chain", []))
with open(crt_path, "w") as fh:
    fh.write("\n".join(chain) + "\n")
with open(key_path, "w") as fh:
    fh.write(data["private_key"] + "\n")
' "$crt" "$key"
}

if issue_from_ca; then
  echo "==> Issued ${LDAP_CN} from ${BAO_ADDR}/${PKI_MOUNT} (role ${PKI_ROLE})"
else
  echo "==> Lab CA unreachable at ${BAO_ADDR}; falling back to a self-signed certificate" >&2
  echo "    A verifying LDAP client will reject it. Set LDAP_URI to ldap:// for the" >&2
  echo "    fixture, or start the CA first and re-run this script." >&2
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
    -keyout "$key" -out "$crt" \
    -subj "/CN=${LDAP_CN}" \
    -addext "subjectAltName=DNS:${LDAP_CN},DNS:lab-ldap,DNS:localhost,IP:127.0.0.1" 2>/dev/null
fi

# glauth runs unprivileged and only needs to read these.
chmod 644 "$crt" "$key"
echo "==> Wrote ${crt} and ${key}"
