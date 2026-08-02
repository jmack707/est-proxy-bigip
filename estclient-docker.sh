#!/usr/bin/env bash
#
# Run the libest `estclient` from a container.
#
# For hosts whose distribution has no libest-utils package — anything before
# Ubuntu 25.10. Arguments are passed straight through to estclient, so this is
# a drop-in substitute for the binary. See ADR-0006.
#
#   ./estclient-docker.sh -g -s est.example.com -p 8443 -o /out
#   ./estclient-docker.sh -e -s est.example.com -p 8443 -o /out \
#       -x /out/client.key --common-name client.example.com
#
# Inside the container: output is /out, the CA chain is /ca-chain.pem. Keep
# client keys and certificates in the output directory so they are reachable
# at /out/<name> for -x, -c, and -k.

set -euo pipefail

CONTAINER_CLI="${CONTAINER_CLI:-docker}"
ESTCLIENT_IMAGE="${ESTCLIENT_IMAGE:-estclient-tool}"
ESTCLIENT_BASE_IMAGE="${ESTCLIENT_BASE_IMAGE:-ubuntu:26.04}"
ESTCLIENT_OUT="${ESTCLIENT_OUT:-${PWD}/est-out}"
ESTCLIENT_CACERT="${EST_OPENSSL_CACERT:-${PWD}/ca-chain.pem}"
ESTCLIENT_ADD_HOST="${ESTCLIENT_ADD_HOST:-}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v "${CONTAINER_CLI}" >/dev/null 2>&1; then
  echo "estclient-docker.sh: '${CONTAINER_CLI}' not found; set CONTAINER_CLI=podman if that is what you run" >&2
  exit 2
fi

if [ ! -f "${ESTCLIENT_CACERT}" ]; then
  echo "estclient-docker.sh: CA chain not found at ${ESTCLIENT_CACERT}" >&2
  echo "  set EST_OPENSSL_CACERT to its path, or fetch it:" >&2
  echo "  curl -s \"\$BAO_ADDR/v1/\$PKI_MOUNT/ca_chain\" > ca-chain.pem" >&2
  exit 2
fi

if [ "${ESTCLIENT_REBUILD:-0}" = "1" ] \
   || ! "${CONTAINER_CLI}" image inspect "${ESTCLIENT_IMAGE}" >/dev/null 2>&1; then
  echo "==> Building ${ESTCLIENT_IMAGE} from ${ESTCLIENT_BASE_IMAGE}" >&2
  "${CONTAINER_CLI}" build \
    --build-arg "BASE_IMAGE=${ESTCLIENT_BASE_IMAGE}" \
    -t "${ESTCLIENT_IMAGE}" \
    -f "${script_dir}/Dockerfile.estclient" \
    "${script_dir}" >&2
fi

mkdir -p "${ESTCLIENT_OUT}"

run_args=(
  --rm
  -v "${ESTCLIENT_OUT}:/out"
  -v "${ESTCLIENT_CACERT}:/ca-chain.pem:ro"
  -e EST_OPENSSL_CACERT=/ca-chain.pem
)

# The container has its own /etc/hosts; an entry added on the host does not
# reach it. Required whenever the virtual server hostname is not in DNS.
if [ -n "${ESTCLIENT_ADD_HOST}" ]; then
  run_args+=(--add-host "${ESTCLIENT_ADD_HOST}")
fi

exec "${CONTAINER_CLI}" run "${run_args[@]}" "${ESTCLIENT_IMAGE}" "$@"
