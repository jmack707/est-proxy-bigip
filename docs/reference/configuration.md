# Configuration reference

## Overview

Two independent configuration files, read by different things at different times:

| File | Read by | When | Mode |
|---|---|---|---|
| `est-shim.env` (from `est-shim.env.example`) | `est_shim.py`, via the systemd unit's `EnvironmentFile` or `docker run --env-file` | every shim start | `0600` — contains an AppRole secret |
| `deploy.env` (from `deploy.env.example`) | `quickstart.sh` | one-off, at deploy time | `0600` — contains the BIG-IP password |

`quickstart.sh` generates `est-shim.env` from `deploy.env`, so in the quickstart path you edit `deploy.env` only. Values below are read with `os.environ.get()` unless marked required, and a required variable with no value raises `KeyError` at startup rather than falling back to a default.

Precedence inside a container: `--env-file` values are overridden by explicit `-e` flags. Nothing is read from a config file on disk by `est_shim.py` itself.

## est-shim.env — backend (`est_shim.py`)

| Name | Type | Default | Required | Effect |
|---|---|---|---|---|
| `BAO_ADDR` | url | `https://127.0.0.1:8200` | no | Vault/OpenBao API base. TLS to this endpoint is **not verified** — see [ADR-0005](../adr/0005-unverified-tls-to-the-pki-backend.md). Inside a container, `127.0.0.1` is the container itself — when both run under compose, use the service name (`http://openbao:8200`), which is what `quickstart.sh` writes. |
| `BAO_ROLE_ID` | string | none | **yes** | AppRole `role_id` used for `auth/approle/login`. Absent means the process exits at import. |
| `BAO_SECRET_ID` | string (**secret**) | none | **yes** | AppRole `secret_id`. Supply from your secret store or `--env-file`; never commit a value. |
| `PKI_MOUNT` | string | `pki_int` | no | PKI secrets-engine mount path. Used for `<mount>/ca_chain`, `<mount>/sign/<role>`, `<mount>/issue/<role>`. |
| `PKI_ROLE` | string | `example-dot-com` | no | Signing role. Its `allowed_domains` decides which CNs can be issued; a CN outside it fails at the backend, not in the shim. |
| `DOMAIN` | string | `example.com` | no | Only used to build the `serverkeygen` CN (`est-serverkeygen.<DOMAIN>`), since that operation generates the subject itself. |
| `LISTEN_PORT` | int | `8085` | no | Plain-HTTP listener, bound to `0.0.0.0`. Must match the BIG-IP pool member port. In the compose path the published port is fixed at `8085`. |
| `LDAP_ENABLED` | bool (`true`/`false`, case-insensitive) | `false` | no | Master switch for directory-gated enrollment. When `false`, `ldap3` is never imported. |
| `LDAP_URI` | url | `ldaps://127.0.0.1:636` | no | Directory endpoint. A `ldaps://` prefix turns on TLS for the connection. |
| `LDAP_BIND_DN_TEMPLATE` | string | `{username}` | when `LDAP_ENABLED` | Bind DN with `{username}` substituted from HTTP Basic auth. FreeIPA: `uid={username},cn=users,cn=accounts,dc=…`. AD: `{username}@<domain>` (UPN) or `CN={username},CN=Users,DC=…`. |
| `LDAP_START_TLS` | bool | `false` | no | Upgrade a plain `ldap://` connection with STARTTLS before binding. Irrelevant, and left off, for `ldaps://`. |
| `LDAP_REQUIRE_OPS` | comma-separated list | `simpleenroll,serverkeygen` | no | Which EST operations require a successful bind. Both issuing operations are in the default; `simplereenroll` is deliberately excluded because it authenticates with the existing certificate over TLS ([ADR-0001](../adr/0001-terminate-tls-at-the-virtual-server.md), [ADR-0008](../adr/0008-do-not-trust-proxy-supplied-identity-unconditionally.md)). |
| `LDAP_ENFORCE_CN_MATCH` | bool | `true` | no | Reject when the CSR's CN differs from the authenticated username, so one valid credential cannot mint a certificate naming somebody else. The comparison is **exact string equality**, so with this on the username must itself be a name the PKI role will sign — see [troubleshooting](../operations/troubleshooting.md#with-cn-enforcement-on-the-username-is-the-certificate-name) before naming accounts. Set `false` only when CNs are intentionally unrelated to usernames — a shared service account enrolling device hostnames, for example. |
| `LDAP_CA_FILE` | path | none | no | CA bundle used to verify the directory's TLS certificate. Unset leaves `ldap3` at its `CERT_NONE` default, which encrypts the session without authenticating the server. When set, `LDAP_URI` must use a name the directory's certificate carries — an IP address fails hostname verification. |

## est-shim.env — proxy trust and rate limiting

Added by [ADR-0008](../adr/0008-do-not-trust-proxy-supplied-identity-unconditionally.md) after two bypasses were demonstrated against a live deployment. Defaults are safe; the one value you must set deliberately is `EST_PROXY_SECRET`.

| Name | Type | Default | Required | Effect |
|---|---|---|---|---|
| `EST_PROXY_SECRET` | string (**secret**) | none | no, but strongly recommended | Shared secret the `est_proxy` iRule presents as `X-EST-Proxy-Secret`. When set, requests without a matching value are refused `403`. Deploy the same value with [`deploy_bigip.py --proxy-secret`](cli.md#deploy_bigippy). Unset means the shim cannot distinguish a proxied request from one that reached the port directly, and the `X-SSL-Client-*` headers become attacker-supplied. |
| `REENROLL_VERIFY_CERT` | bool | `true` | no | Verify the forwarded client certificate against the issuing chain on `simplereenroll`, and require the CSR to match it. Setting `false` restores the behaviour in which any request carrying the header is treated as authenticated. |
| `AUTH_MAX_FAILURES` | int | `5` | no | Failed binds per username inside the window before the shim answers `429` with `Retry-After`. `0` disables. Counted in this process only. |
| `AUTH_WINDOW_SECONDS` | int | `300` | no | Length of that window. |

The throttle is keyed on the username alone. Behind SNAT every request arrives from one address, so keying on the caller would not separate them — meaning someone who knows a username can hold it throttled. That is deliberate: a local, self-clearing refusal is preferred to lockout of the account in the real directory.

## deploy.env — quickstart (`quickstart.sh`)

`LDAP_ENABLED`, `LDAP_URI`, `LDAP_BIND_DN_TEMPLATE`, `LDAP_START_TLS`, and `LDAP_ENFORCE_CN_MATCH` carry the meanings above and are passed through into the generated `est-shim.env`. `LDAP_REQUIRE_OPS` is not settable here; it takes the shim default. `DOMAIN` does more here than in the shim, so it gets its own row below.

| Name | Type | Default | Required | Effect |
|---|---|---|---|---|
| `DOMAIN` | domain | none | **yes** | Names the lab PKI signing role (dots become dashes) and sets that role's `allowed_domains` (subdomains allowed), so it decides which CNs the quickstart PKI can issue at all — `VS_HOSTNAME` must equal it or be a subdomain of it. Also passed through as the shim's `DOMAIN`. |
| `BACKEND_HOST` | ip/hostname | none | **yes** | Address the BIG-IP pool member will use to reach the shim — this host's routable address, never `127.0.0.1`. |
| `BIGIP_HOST` | ip/hostname | none | **yes** | BIG-IP management address for iControl REST. |
| `BIGIP_USER` | string | none | **yes** | BIG-IP account with rights to create LTM objects and install crypto objects. |
| `BIGIP_PASS` | string (**secret**) | none | **yes** | Password for that account. Passed on the command line to the deploy scripts, so it is visible in the process table while they run. |
| `VS_DESTINATION` | `ip:port` | none | **yes** | Virtual server listener, e.g. `10.1.10.100:8443`. |
| `VS_VLAN` | `/Partition/name` | none | **yes** | VLAN the virtual server listens on. |
| `VS_HOSTNAME` | fqdn | none | **yes** | CN/SAN for the virtual server's own bootstrap certificate, and the name `estclient -s` must use. Must resolve to `VS_DESTINATION`'s address on the test client, and must satisfy the PKI role's `allowed_domains` — `quickstart.sh` checks this up front rather than letting the backend fail with an opaque message. |

`quickstart.sh` requires `DOMAIN`, `BACKEND_HOST`, `BIGIP_HOST`, `BIGIP_USER`, `BIGIP_PASS`, `VS_DESTINATION`, `VS_VLAN`, and `VS_HOSTNAME` to be non-empty and exits if any is missing.

`BIGIP_CA_FILE` is read from the environment by `deploy_bigip.py`, `install-cert-bigip.py`, and `bigip-est-enroll.py` (not from `deploy.env`). Set it to a CA bundle that signs the BIG-IP management certificate to verify iControl REST; unset, the management TLS is not verified, which a self-signed BIG-IP needs but which exposes the management channel to a MITM. Verify it in production.

## Lab directory fixture (`docker-compose.lab-ldap.yml`)

A throwaway LDAP server for exercising the directory gate where no directory exists. **Lab-only**, off by default, and it deliberately proves less than it appears to — read [ADR-0007](../adr/0007-bundled-lab-directory-for-gate-testing.md) before relying on a passing run.

```bash
./lab-ldap/gen-cert.sh
docker compose -f docker-compose.yml -f docker-compose.lab-ldap.yml \
  --profile lab-ldap up -d
```

Seeded users are `client1.example.com` and `client2.example.com`, both with password `estlab123`, as SHA-256 hashes committed in `lab-ldap/glauth.cfg`. That is safe only because this is a fixture, on the same reasoning as dev-mode OpenBao's known root token.

Point the shim at it with the existing variables — the fixture introduces none of its own:

| Variable | Value for the fixture |
|---|---|
| `LDAP_ENABLED` | `true` |
| `LDAP_URI` | `ldaps://lab-ldap.example.com:3894` |
| `LDAP_BIND_DN_TEMPLATE` | `uid={username},cn=users,cn=accounts,dc=example,dc=com` |
| `LDAP_START_TLS` | `false` — the fixture serves LDAPS directly |

Three names must agree if you change the domain: the `baseDN` and user entries in `lab-ldap/glauth.cfg`, the network alias in `docker-compose.lab-ldap.yml`, and `LDAP_CN` in `lab-ldap/gen-cert.sh`.

`lab-ldap/gen-cert.sh` issues the LDAPS certificate from the lab CA so the fixture is reached over *verified* TLS rather than with verification disabled:

| Variable | Default | Effect |
|---|---|---|
| `BAO_ADDR` | `http://127.0.0.1:8200` | CA to request the certificate from |
| `BAO_TOKEN` | `root` | Dev-mode root token ([ADR-0004](../adr/0004-dev-mode-openbao-for-lab-bootstrap.md)) |
| `PKI_MOUNT` / `PKI_ROLE` | `pki_int` / `example-dot-com` | Mount and signing role |
| `LDAP_CN` | `lab-ldap.example.com` | CN and the name the shim connects to. Must satisfy the role's `allowed_domains` |

It falls back to a self-signed certificate when the CA is unreachable and says so. A verifying LDAP client rejects that, so start the CA first rather than working around it.

## bootstrap-openbao-dev.sh environment

| Name | Type | Default | Required | Effect |
|---|---|---|---|---|
| `BAO_ADDR` | url | `http://127.0.0.1:8210` | no | OpenBao API base the bootstrap talks to. **Note the default port differs from `docker-compose.yml`, which publishes `8200`** — pass `BAO_ADDR` explicitly, as the documented invocations do. |
| `BAO_TOKEN` | string (**secret**) | `root` | no | Dev-mode root token. The default is only safe because dev mode is lab-only ([ADR-0004](../adr/0004-dev-mode-openbao-for-lab-bootstrap.md)). |

Positional arguments: `$1` domain (default `example.com`), `$2` role name (default: the domain with dots replaced by dashes).
