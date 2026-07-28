# Install

Installs the EST backend (`est_shim.py`) and the operator tooling. BIG-IP objects are covered in [deploy](deploy.md).

## Prerequisites

Last validated: 2026-07 — BIG-IP VE 21.1, OpenBao 2.2.0, FreeIPA (LDAPS), `estclient` from `libest-utils`.

Versions in the first column are what the project has been validated against, not minimums pulled from a compatibility matrix.

| Requirement | Version tested | Notes |
|---|---|---|
| BIG-IP VE | 21.1 | Needs `client-ssl`, iRules, and iControl REST access |
| PKI backend | OpenBao 2.2.0 | Any Vault-API-compatible PKI with `ca_chain`, `sign/<role>`, `issue/<role>` |
| Python (host install) | 3.9+ | `est_shim.py` is stdlib-only apart from `ldap3` |
| Python (container) | 3.12-slim | Base image in the `Dockerfile` |
| `openssl` CLI | distribution default | **Hard dependency** — PKCS#7 degenerate packaging and CSR parsing shell out to it |
| `ldap3` | `>=2.9,<3` | Only when `LDAP_ENABLED=true`. Pure Python, so no `libldap` or `libsasl` packages |
| `estclient` | libest | Only for testing and for `bigip-est-enroll.py`. Debian/Ubuntu: `apt install libest-utils` |
| Directory | FreeIPA, or Active Directory with LDAPS | Optional; only for gated enrolment |

Network reachability: BIG-IP pool member network → backend host on `LISTEN_PORT` (default `8085`, plain HTTP); backend → PKI backend on its API port; backend → directory on 636 or 389; operator workstation → BIG-IP management interface on 443.

Privileges: a BIG-IP account able to create LTM objects and install `sys crypto` objects; root or a `sudo`-capable account on the backend host for the systemd path; a PKI AppRole scoped to `ca_chain`, `sign/<role>`, and `issue/<role>`.

## Procedure

### 1. Obtain PKI credentials

With an existing Vault or OpenBao, create an AppRole scoped as above and note its `role_id` and `secret_id`. Without one, stand up a lab instance — see [deploy](deploy.md#part-0--a-pki-backend-where-none-exists) and be clear that dev mode is lab-only ([ADR-0004](adr/0004-dev-mode-openbao-for-lab-bootstrap.md)).

### 2. Install the backend

As a host process:

```bash
pip install -r requirements.txt          # only strictly needed when LDAP_ENABLED=true
sudo install -m 0755 est_shim.py /usr/local/bin/est_shim.py
sudo install -d -m 0700 /etc/est-shim
sudo install -m 0600 est-shim.env.example /etc/est-shim/est-shim.env
sudo -e /etc/est-shim/est-shim.env       # fill in every value
sudo cp est-shim.service /etc/systemd/system/
sudo systemctl enable --now est-shim
```

Or as a container:

```bash
docker build -t est-shim .
docker run -d --name est-shim -p 8085:8085 --env-file est-shim.env est-shim
```

Every setting is documented in the [configuration reference](reference/configuration.md#est-shimenv--backend-est_shimpy). `BAO_ROLE_ID` and `BAO_SECRET_ID` have no defaults and the process exits without them.

### 3. Install the operator tooling

On the workstation that will run enrolments:

```bash
sudo apt install libest-utils            # provides estclient
python3 --version                        # 3.9+
```

## Verification

The backend is up and can reach the CA when `cacerts` returns a decodable chain over plain HTTP, before any BIG-IP is involved:

```bash
curl -s http://<backend-host>:8085/.well-known/est/cacerts \
  | base64 -d | openssl pkcs7 -inform DER -print_certs -noout
```

Expected: subject and issuer lines for your root and intermediate. A `502` means the shim is running but cannot reach the PKI — check `BAO_ADDR`, the AppRole, and the mount path. An empty reply means the process is not listening; check `systemctl status est-shim` or `docker logs est-shim`.

With `LDAP_ENABLED=true`, confirm the gate before wiring BIG-IP in:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Content-Type: application/pkcs10' \
  http://<backend-host>:8085/.well-known/est/simpleenroll
```

Expected: `401`. A `502` means the request passed the directory check and failed later, which means the gate is not doing its job.

## Uninstall

```bash
sudo systemctl disable --now est-shim
sudo rm -f /etc/systemd/system/est-shim.service /usr/local/bin/est_shim.py
sudo rm -rf /etc/est-shim
sudo systemctl daemon-reload

# container path
docker rm -f est-shim && docker rmi est-shim
```

Left behind deliberately: certificates already issued by the CA, and BIG-IP objects. Revoke the AppRole `secret_id` at the backend — removing the shim does not invalidate a credential that can still sign certificates. For BIG-IP objects, see [upgrade](upgrade.md#teardown).
