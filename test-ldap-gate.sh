#!/usr/bin/env bash
#
# Assert the directory gate refuses what it should.
#
# Runs the four cases CONTRIBUTING requires before a pull request, against a
# deployment with LDAP_ENABLED=true:
#
#   no credentials                    -> 401
#   wrong password                    -> 403
#   CSR CN not the authenticated user -> 403
#   correct credentials, matching CN  -> 200 and a real certificate
#
# NOTE ON USERNAMES. est_shim.py compares the CSR's CN to the authenticated
# username with exact string equality (`if cn != ldap_username`). The CA
# separately refuses any CN outside the PKI role's allowed_domains. Both hold
# at once only when the directory username IS a name the role permits — so the
# test users are named `client1.example.com`, not `client1`. This is a property
# of LDAP_ENFORCE_CN_MATCH, not of this script.
#
# Talks to the shim directly by default, which isolates the gate from the
# BIG-IP. Point EST_URL at the virtual server to exercise the whole path.
#
# Exits non-zero on the first case that does not match, so it is usable in CI.

set -uo pipefail

EST_URL="${EST_URL:-http://127.0.0.1:8085}"
EST_USER="${EST_USER:-client1.example.com}"
EST_PASS="${EST_PASS:-estlab123}"
EST_OTHER_USER="${EST_OTHER_USER:-client2.example.com}"
CURL_OPTS="${CURL_OPTS:--sk}"
# When the shim runs with EST_PROXY_SECRET, requests that did not come through
# the iRule are refused. Set the same value here to use the shim-direct mode,
# which isolates the gate from the BIG-IP. Leave unset when going through the
# virtual server — the iRule supplies it.
EST_PROXY_SECRET="${EST_PROXY_SECRET:-}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

fail=0

csr_for() {
  # $1 = CN, used verbatim. Emits base64 DER, the encoding EST carries.
  openssl req -new -newkey rsa:2048 -nodes \
    -keyout "$work/$1.key" -subj "/CN=$1" -outform DER -out "$work/$1.der" 2>/dev/null
  base64 "$work/$1.der" > "$work/$1.b64"
}

post_op() {
  # $1 = op, $2 = description, $3 = expected status, $4 = csr file, $5.. = curl args
  local op="$1" desc="$2" expect="$3" csr="$4"; shift 4
  local got secret=()
  [ -n "$EST_PROXY_SECRET" ] && secret=(-H "X-EST-Proxy-Secret: $EST_PROXY_SECRET")
  got="$(curl $CURL_OPTS -o "$work/body" -w '%{http_code}' -X POST \
    -H 'Content-Type: application/pkcs10' \
    -H 'Content-Transfer-Encoding: base64' \
    "${secret[@]}" \
    --data-binary "@$csr" "$@" \
    "$EST_URL/.well-known/est/$op")"
  # expect may be a single code or alternatives like "401|403", for cases whose
  # refusal differs by where it is refused.
  if [[ "|$expect|" == *"|$got|"* ]]; then
    printf '  ok    %-34s %s\n' "$desc" "$got"
  else
    printf '  FAIL  %-34s expected %s, got %s\n' "$desc" "$expect" "$got"
    sed -n '1,3p' "$work/body" | sed 's/^/          /'
    fail=1
  fi
}

post() { post_op simpleenroll "$@"; }

echo "Directory gate, against $EST_URL"
echo "  authenticating as: $EST_USER"

csr_for "$EST_USER"
csr_for "$EST_OTHER_USER"

post "no credentials"      401 "$work/${EST_USER}.b64"
post "wrong password"      403 "$work/${EST_USER}.b64"       -u "${EST_USER}:definitely-not-the-password"
post "CN is another user"  403 "$work/${EST_OTHER_USER}.b64" -u "${EST_USER}:${EST_PASS}"
post "correct credentials" 200 "$work/${EST_USER}.b64"       -u "${EST_USER}:${EST_PASS}"
cp "$work/body" "$work/success-body" 2>/dev/null || true

# --- regression cases for two bypasses found 2026-08 ------------------------
# Both were live against the pre-hardening shim. They are here so a change that
# reintroduces either fails visibly rather than quietly.

# 1. SAN bypass: a CN that passes the username check, with SANs naming others.
#    TLS peers validate against SANs, so honouring these defeats the CN check.
san_cnf="$work/san.cnf"
cat > "$san_cnf" <<EOF
[req]
distinguished_name = dn
req_extensions = ext
prompt = no
[dn]
CN = ${EST_USER}
[ext]
subjectAltName = DNS:${EST_OTHER_USER}
EOF
openssl req -new -newkey rsa:2048 -nodes -keyout "$work/san.key" \
  -config "$san_cnf" -outform DER -out "$work/san.der" 2>/dev/null
base64 "$work/san.der" > "$work/san.b64"
post "SAN names another user" 403 "$work/san.b64" -u "${EST_USER}:${EST_PASS}"

# 2. Forged proxy identity: simplereenroll with a made-up X-SSL-Client-Cert and
#    no credentials at all. Anything but a refusal means the shim is trusting a
#    header any client can set. Pre-hardening this returned 200 and a usable
#    certificate for whatever name the CSR asked for.
post_op simplereenroll "forged client-cert header" "401|403" "$work/${EST_USER}.b64" \
  -H "X-SSL-Client-Cert: totally-made-up"

# The success case must also have produced something decodable — a 200 alone is
# not proof, for the same reason estclient's exit code is not.
if [ "$fail" = "0" ]; then
  if base64 -d "$work/success-body" 2>/dev/null | openssl pkcs7 -inform DER -print_certs -noout >/dev/null 2>&1; then
    echo "  ok    issued certificate decodes"
  else
    echo "  FAIL  issued certificate does not decode"
    fail=1
  fi
fi

[ "$fail" = "0" ] && echo "All gate assertions passed." || echo "Gate assertions FAILED." >&2
exit "$fail"
