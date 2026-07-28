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
| `BAO_ADDR` | url | `https://127.0.0.1:8200` | no | Vault/OpenBao API base. TLS to this endpoint is **not verified** — see [ADR-0005](../adr/0005-unverified-tls-to-the-pki-backend.md). |
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
| `LDAP_REQUIRE_OPS` | comma-separated list | `simpleenroll` | no | Which EST operations require a successful bind. `simplereenroll` is deliberately excluded — see [ADR-0001](../adr/0001-terminate-tls-at-the-virtual-server.md). |
| `LDAP_ENFORCE_CN_MATCH` | bool | `true` | no | Reject when the CSR's CN differs from the authenticated username, so one valid credential cannot mint a certificate naming somebody else. Set `false` only when CNs are intentionally unrelated to usernames — a shared service account enrolling device hostnames, for example. |

## deploy.env — quickstart (`quickstart.sh`)

`DOMAIN`, `LDAP_ENABLED`, `LDAP_URI`, `LDAP_BIND_DN_TEMPLATE`, `LDAP_START_TLS`, and `LDAP_ENFORCE_CN_MATCH` carry the meanings above and are passed through into the generated `est-shim.env`. `LDAP_REQUIRE_OPS` is not settable here; it takes the shim default.

| Name | Type | Default | Required | Effect |
|---|---|---|---|---|
| `BACKEND_HOST` | ip/hostname | none | **yes** | Address the BIG-IP pool member will use to reach the shim — this host's routable address, never `127.0.0.1`. |
| `BIGIP_HOST` | ip/hostname | none | **yes** | BIG-IP management address for iControl REST. |
| `BIGIP_USER` | string | none | **yes** | BIG-IP account with rights to create LTM objects and install crypto objects. |
| `BIGIP_PASS` | string (**secret**) | none | **yes** | Password for that account. Passed on the command line to the deploy scripts, so it is visible in the process table while they run. |
| `VS_DESTINATION` | `ip:port` | none | **yes** | Virtual server listener, e.g. `10.1.10.100:8443`. |
| `VS_VLAN` | `/Partition/name` | none | **yes** | VLAN the virtual server listens on. |
| `VS_HOSTNAME` | fqdn | none | **yes** | CN/SAN for the virtual server's own bootstrap certificate, and the name `estclient -s` must use. Must resolve to `VS_DESTINATION`'s address on the test client, and must satisfy the PKI role's `allowed_domains` — `quickstart.sh` checks this up front rather than letting the backend fail with an opaque message. |

`quickstart.sh` requires `DOMAIN`, `BACKEND_HOST`, `BIGIP_HOST`, `BIGIP_USER`, `BIGIP_PASS`, `VS_DESTINATION`, `VS_VLAN`, and `VS_HOSTNAME` to be non-empty and exits if any is missing.

## bootstrap-openbao-dev.sh environment

| Name | Type | Default | Required | Effect |
|---|---|---|---|---|
| `BAO_ADDR` | url | `http://127.0.0.1:8210` | no | OpenBao API base the bootstrap talks to. **Note the default port differs from `docker-compose.yml`, which publishes `8200`** — pass `BAO_ADDR` explicitly, as the documented invocations do. |
| `BAO_TOKEN` | string (**secret**) | `root` | no | Dev-mode root token. The default is only safe because dev mode is lab-only ([ADR-0004](../adr/0004-dev-mode-openbao-for-lab-bootstrap.md)). |

Positional arguments: `$1` domain (default `example.com`), `$2` role name (default: the domain with dots replaced by dashes).
