#!/usr/bin/env bash
# Reverses quickstart.sh: removes the BIG-IP objects it created and stops the
# backend stack. Reads the same deploy.env, so the two stay in step.
#
# Usage:
#   ./teardown.sh --yes                  # BIG-IP objects + backend containers
#   ./teardown.sh --yes --bigip-only     # leave the backend running
#   ./teardown.sh --yes --backend-only   # leave the BIG-IP untouched
#   ./teardown.sh --yes --purge          # also delete generated files
#
# Destructive, so it refuses to run without --yes.
#
# Idempotent: an object that is already gone is reported and skipped, so a
# partial teardown can be re-run. Deletion order is not arbitrary -- BIG-IP
# refuses to delete an object that is still referenced, so the virtual server
# goes first, and the client-ssl profile's cert-key-chain is reset back to the
# default pair before the certificate and key it pinned can be removed.
set -euo pipefail
cd "$(dirname "$0")"

YES=0; DO_BIGIP=1; DO_BACKEND=1; PURGE=0
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    --bigip-only) DO_BACKEND=0 ;;
    --backend-only) DO_BIGIP=0 ;;
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "ERROR: unknown argument '$arg' (see --help)" >&2; exit 2 ;;
  esac
done

[ -f deploy.env ] || { echo "ERROR: deploy.env not found -- nothing to tear down from." >&2; exit 1; }
# shellcheck disable=SC1091
source deploy.env

# Object names match deploy_bigip.py's defaults, which is what quickstart.sh
# uses. Override in deploy.env if you deployed with custom names.
POOL_NAME="${POOL_NAME:-est-backend-pool}"
CLIENTSSL_PROFILE="${CLIENTSSL_PROFILE:-est-clientssl}"
IRULE_NAME="${IRULE_NAME:-est_proxy}"
VS_NAME="${VS_NAME:-est-proxy-vs}"
VS_CERT_NAME="${VS_CERT_NAME:-est-proxy-vs-cert}"

if [ "$YES" != 1 ]; then
  echo "This removes:"
  [ "$DO_BIGIP" = 1 ] && cat <<EOF
  on BIG-IP ${BIGIP_HOST:-<BIGIP_HOST unset>}:
    ltm virtual $VS_NAME
    ltm rule $IRULE_NAME
    ltm profile client-ssl $CLIENTSSL_PROFILE
    ltm pool $POOL_NAME
    sys crypto cert/key $VS_CERT_NAME
EOF
  [ "$DO_BACKEND" = 1 ] && echo "  locally: the est-shim and openbao containers, with their volumes"
  [ "$PURGE" = 1 ] && echo "  locally: est-shim.env and ca-chain.pem"
  echo
  echo "Re-run with --yes to proceed."
  exit 3
fi

if [ "$DO_BIGIP" = 1 ]; then
  for v in BIGIP_HOST BIGIP_USER BIGIP_PASS; do
    [ -n "${!v:-}" ] || { echo "ERROR: $v is not set in deploy.env" >&2; exit 1; }
  done

  # tmsh over iControl REST -- the same path bigip_lib.py uses, so teardown
  # needs no SSH access, only the management credentials the deploy already had.
  tmsh_run() {
    local cmd="$1"
    local payload
    payload="$(python3 -c 'import json,sys; print(json.dumps({"command":"run","utilCmdArgs":"-c \"%s\"" % sys.argv[1]}))' "$cmd")"
    curl -sk -u "$BIGIP_USER:$BIGIP_PASS" -H "Content-Type: application/json" \
      -X POST "https://$BIGIP_HOST/mgmt/tm/util/bash" -d "$payload" \
      | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("commandResult", ""), end="")
except Exception:
    pass'
  }

  drop() {
    local desc="$1" cmd="$2"
    local out
    out="$(tmsh_run "$cmd" 2>&1 || true)"
    if [ -z "$out" ]; then
      echo "removed: $desc"
    elif echo "$out" | grep -qiE "was not found|not found|does not exist"; then
      echo "already gone (ok): $desc"
    else
      echo "WARNING: $desc -- ${out%$'\n'}" >&2
    fi
  }

  echo "==> Removing BIG-IP objects from $BIGIP_HOST"
  # Detach the bootstrap cert/key first; BIG-IP will not delete either while a
  # profile still pins them in its cert-key-chain.
  drop "client-ssl $CLIENTSSL_PROFILE cert-key-chain reset" \
    "tmsh modify ltm profile client-ssl $CLIENTSSL_PROFILE cert-key-chain replace-all-with { default { cert default.crt key default.key } }"
  drop "ltm virtual $VS_NAME"                        "tmsh delete ltm virtual $VS_NAME"
  drop "ltm rule $IRULE_NAME"                        "tmsh delete ltm rule $IRULE_NAME"
  drop "ltm profile client-ssl $CLIENTSSL_PROFILE"   "tmsh delete ltm profile client-ssl $CLIENTSSL_PROFILE"
  drop "ltm pool $POOL_NAME"                         "tmsh delete ltm pool $POOL_NAME"
  drop "sys crypto key $VS_CERT_NAME"                "tmsh delete sys crypto key $VS_CERT_NAME"
  drop "sys crypto cert $VS_CERT_NAME"               "tmsh delete sys crypto cert $VS_CERT_NAME"
  drop "save running config"                         "tmsh save sys config"
fi

if [ "$DO_BACKEND" = 1 ]; then
  echo "==> Stopping the backend stack"
  if docker compose version >/dev/null 2>&1; then
    docker compose down -v
  else
    echo "WARNING: 'docker compose' not available here -- stop the stack manually." >&2
  fi
fi

if [ "$PURGE" = 1 ]; then
  echo "==> Removing generated files"
  rm -f est-shim.env ca-chain.pem
  echo "removed: est-shim.env, ca-chain.pem"
fi

cat <<EOF

==> Teardown complete.

Deliberately left alone:
  - Certificates already issued by the CA. Dev-mode OpenBao is in-memory, so
    tearing down the backend destroys the CA with it and those certificates
    can no longer be verified against it -- but they are still out there.
  - If you pointed the shim at an OpenBao or Vault you run elsewhere, its
    AppRole secret_id is still valid and can still sign. Revoke it there;
    removing the shim does not.
  - deploy.env, so you can redeploy with the same values.
EOF
