# CLI reference

## Overview

Eight entry points. Run them in this order the first time:

| Script | Role |
|---|---|
| [`quickstart.sh`](#quickstartsh) | runs the four below end to end from one config file — start here |
| [`teardown.sh`](#teardownsh) | reverses `quickstart.sh` |
| [`bootstrap-openbao-dev.sh`](#bootstrap-openbao-devsh) | stands up a throwaway PKI when no Vault/OpenBao exists |
| [`est_shim.py`](#est_shimpy) | the EST server itself |
| [`deploy_bigip.py`](#deploy_bigippy) | creates the BIG-IP pool, profile, iRule, and virtual server |
| [`install-cert-bigip.py`](#install-cert-bigippy) | installs a PEM cert/key pair you already hold |
| [`bigip-est-enroll.py`](#bigip-est-enrollpy) | obtains a certificate *for* a BIG-IP via a real EST exchange |
| [`test-ldap-gate.sh`](#test-ldap-gatesh) | asserts the directory gate refuses what it should |

`bigip_lib.py` is a shared iControl REST helper module, not an entry point; it is imported by the two cert-installing scripts.

## `quickstart.sh`

Drives the [deploy fast path](../deploy.md#fast-path) non-interactively: dev-mode OpenBao, PKI bootstrap, shim configuration and start, BIG-IP object deploy, and the virtual server's own bootstrap certificate. Directory setup ([AD/LDAPS setup](../operations/ad-setup.md)) and testing with `estclient` stay manual.

```bash
cp deploy.env.example deploy.env    # fill in every value
./quickstart.sh
```

Reads `deploy.env` from the working directory; takes no flags. Re-running is safe: the OpenBao bootstrap and the BIG-IP deploy are both idempotent, and the bootstrap certificate step re-issues a fresh certificate each run. Exits non-zero when a required variable is empty or when `VS_HOSTNAME` falls outside the PKI role's `allowed_domains`.

## `teardown.sh`

Reverses `quickstart.sh`, reading the same `deploy.env` so the two stay in step. Destructive, so it refuses to run without `--yes` and prints what it would remove instead.

```bash
./teardown.sh --yes                  # BIG-IP objects + backend containers
./teardown.sh --yes --bigip-only     # leave the backend running
./teardown.sh --yes --backend-only   # leave the BIG-IP untouched
./teardown.sh --yes --purge          # also delete est-shim.env and ca-chain.pem
```

| Flag | Effect |
|---|---|
| `--yes` | required; without it the script lists what it would remove and exits `3` |
| `--bigip-only` | skip `docker compose down -v` |
| `--backend-only` | skip the BIG-IP objects; `BIGIP_*` need not be set |
| `--purge` | also delete the generated `est-shim.env` and `ca-chain.pem` |

Object names default to `deploy_bigip.py`'s defaults, which is what `quickstart.sh` deploys. If you deployed with custom names, set `POOL_NAME`, `CLIENTSSL_PROFILE`, `IRULE_NAME`, `VS_NAME`, or `VS_CERT_NAME` in `deploy.env`.

Removal order is not arbitrary: BIG-IP refuses to delete an object that is still referenced, so the virtual server goes first, and the client-ssl profile's `cert-key-chain` is reset to the default pair before the certificate and key it pinned can be deleted. Objects that are already gone are reported and skipped, so a partial teardown can be re-run.

It works over iControl REST using the same management credentials as the deploy — no SSH access to the BIG-IP is needed.

Deliberately left behind: `deploy.env`, and certificates the CA already issued. If the shim pointed at a Vault/OpenBao you run elsewhere, its AppRole `secret_id` remains valid and must be revoked there.

Exit codes: `0` success, `1` missing `deploy.env` or required variable, `2` bad argument, `3` refused for want of `--yes`.

## `bootstrap-openbao-dev.sh`

Mounts a root CA and an intermediate, signs and sets the intermediate, creates a signing role, and creates an AppRole scoped for the shim. Prints `BAO_ROLE_ID`, `BAO_SECRET_ID`, and `PKI_ROLE` for pasting into `est-shim.env`.

```bash
BAO_ADDR=http://127.0.0.1:8200 ./bootstrap-openbao-dev.sh your-domain.com
```

| Argument | Position | Default | Effect |
|---|---|---|---|
| domain | `$1` | `example.com` | CA common names and the role's `allowed_domains` |
| role | `$2` | domain with dots as dashes | PKI signing role name |

Environment: `BAO_ADDR`, `BAO_TOKEN` — see the [configuration reference](configuration.md#bootstrap-openbao-devsh-environment). Runs under `set -euo pipefail`, so it stops at the first failing API call.

## `est_shim.py`

The EST server. No flags; configured entirely by environment ([reference](configuration.md#est-shimenv--backend-est_shimpy)). Listens plain HTTP on `LISTEN_PORT` behind the virtual server.

```bash
# host process, via systemd
sudo cp est-shim.service /etc/systemd/system/
sudo systemctl enable --now est-shim

# container
docker run -d --name est-shim -p 8085:8085 --env-file est-shim.env est-shim
```

Logs one line per request to stderr — journald for the systemd path, `docker logs` for the container path. Exits immediately if `BAO_ROLE_ID` or `BAO_SECRET_ID` is unset.

## `deploy_bigip.py`

Creates the pool, a `client-ssl` profile with `peerCertMode: request`, the iRule, and the virtual server with SNAT automap, over iControl REST.

```bash
python3 deploy_bigip.py <bigip-mgmt-host> <user> <password> est_proxy.irule.tcl \
  --pool-member <backend-host>:8085 --vs-destination <vs-listener-ip>:8443 \
  --vs-vlan /Common/<your-vlan>
```

| Argument | Kind | Default | Effect |
|---|---|---|---|
| `host` | positional | — | BIG-IP management host or IP |
| `user` | positional | — | iControl REST account |
| `password` | positional | — | password for that account |
| `irule_file` | positional | — | path to `est_proxy.irule.tcl` |
| `--pool-member` | required, `HOST:PORT` | — | shim address; validated to contain `:` |
| `--vs-destination` | required, `IP:PORT` | — | virtual server listener; validated to contain `:` |
| `--vs-vlan` | optional | `/Common/external` | VLAN the virtual server listens on |
| `--pool-name` | optional | `est-backend-pool` | override to run more than one instance side by side |
| `--clientssl-profile` | optional | `est-clientssl` | as above |
| `--irule-name` | optional | `est_proxy` | as above; must match the pool name baked into the iRule's `static::est_label_pools` if you change both |
| `--vs-name` | optional | `est-proxy-vs` | as above |
| `--proxy-secret` | optional | empty | Substituted into the iRule as `static::est_proxy_secret` and sent to the backend as `X-EST-Proxy-Secret`. Must equal the shim's `EST_PROXY_SECRET` ([ADR-0008](../adr/0008-do-not-trust-proxy-supplied-identity-unconditionally.md)). Rejected if it contains quotes or backslashes, since it is inlined into Tcl |

Object names may be given bare (`clientssl`) or fully qualified (`/Common/clientssl`, or another partition); both reach the same object. This applies to `--vs-vlan`, `--pool-name`, `--clientssl-profile`, `--irule-name`, and `--vs-destination`. Earlier revisions prefixed `/Common/` unconditionally, so a qualified value became `/Common/Common/...` and failed with a `404` naming an object nobody asked for.

Re-running is safe, and for two objects it repairs rather than merely reports:

| Object | On a re-run |
|---|---|
| pool | members reconciled — a pool that exists without the member gets it |
| iRule | body reconciled — an edited iRule is actually uploaded |
| client-ssl profile | reported and left alone: `install-cert-bigip.py` owns its `cert-key-chain`, and a re-run must not disturb the certificate the virtual server is serving |
| virtual server | reported and left alone: changing the listener or profile set under live traffic is an operator decision, not a side effect of a re-run |

The two reconciled objects are the ones this script owns the contents of. Earlier revisions skipped every existing object, so a pool that already existed stayed memberless and an edited iRule was never uploaded — while the run reported success either way.

Exit codes: `0` success, `1` validation or API failure with the reason on stderr, including an existing object that could not be reconciled.

## `install-cert-bigip.py`

Installs a PEM certificate and key as named `sys crypto` objects, optionally attaching them to a `client-ssl` profile. Used for the virtual server's own bootstrap certificate, which cannot come through EST because the virtual server is what serves EST.

```bash
python3 install-cert-bigip.py --bigip-host <bigip-mgmt-ip> --bigip-user admin \
  --bigip-pass '<password>' --cert-name <object-name> \
  --cert-file vs.crt --key-file vs.key --attach-profile est-clientssl
```

| Flag | Required | Effect |
|---|---|---|
| `--bigip-host`, `--bigip-user`, `--bigip-pass` | yes | iControl REST target and credentials |
| `--cert-name` | yes | `sys crypto cert`/`key` object name |
| `--cert-file`, `--key-file` | yes | PEM inputs |
| `--attach-profile` | no | also repoint this `client-ssl` profile's `cert-key-chain` |

Installing over an existing object name is handled by a detach → delete → install → reattach sequence rather than an in-place overwrite; the reason is in the [troubleshooting guide](../operations/troubleshooting.md#reinstalling-a-cert-over-an-existing-name-breaks-the-certkey-pairing).

`--cert-name` and `--attach-profile` must be plain BIG-IP object names (`^[A-Za-z0-9._-]+$`); anything else is refused, because these names are interpolated into `tmsh` commands that run as root on the device ([ADR-0008](../adr/0008-do-not-trust-proxy-supplied-identity-unconditionally.md), addendum). Set `BIGIP_CA_FILE` to verify the management TLS.

## `bigip-est-enroll.py`

Performs a real EST exchange with `estclient`, then installs the result on a BIG-IP. Needs the `estclient` binary (`apt install libest-utils`) and reachability to both the EST virtual server and the BIG-IP management interface.

```bash
python3 bigip-est-enroll.py enroll \
  --est-host <vs-hostname> --est-port 8443 --est-cacert ca-chain.pem \
  --common-name <bigip-fqdn> \
  --bigip-host <bigip-mgmt-ip> --bigip-user admin --bigip-pass '<password>' \
  --cert-name <object-name> --attach-profile <clientssl-profile> --save-dir ./saved

python3 bigip-est-enroll.py renew \
  --est-host <vs-hostname> --est-port 8443 --est-cacert ca-chain.pem \
  --existing-cert ./saved/<object-name>.pem --existing-key ./saved/<object-name>.key \
  --bigip-host <bigip-mgmt-ip> --bigip-user admin --bigip-pass '<password>' \
  --cert-name <object-name> --attach-profile <clientssl-profile>
```

| Flag | Required | Effect |
|---|---|---|
| `mode` | positional, `enroll` or `renew` | first issuance, or RFC 7030 `simplereenroll` |
| `--est-host` | yes | EST server hostname; must match the certificate the virtual server presents |
| `--est-port` | no, default `8443` | EST server port |
| `--est-cacert` | yes | CA chain PEM, exported as `EST_OPENSSL_CACERT` for the client |
| `--common-name` | for `enroll` | CN requested in the CSR |
| `--existing-cert`, `--existing-key` | for `renew` | the previously issued pair, used as TLS client identity |
| `--ldap-user`, `--ldap-password` | when the server gates enrollment | HTTP Basic credentials |
| `--save-dir` | no | save the issued pair for a future `renew` |
| `--bigip-host`, `--bigip-user`, `--bigip-pass` | yes | install target |
| `--cert-name` | yes | `sys crypto` object name |
| `--attach-profile` | no | update this profile's `cert-key-chain` |

For the scheduled version of this, see the [certificate renewal runbook](../operations/runbooks/renew-bigip-certificate.md).

## `test-ldap-gate.sh`

Runs the four assertions [contributing](../../CONTRIBUTING.md) requires of a gated deployment, and exits non-zero on the first mismatch so it is usable in CI. Requires `LDAP_ENABLED=true`; against an ungated deployment every case returns `200` and the run fails, which is the correct answer.

```bash
./test-ldap-gate.sh                                    # straight at the shim
EST_URL=https://<vs-hostname>:8443 ./test-ldap-gate.sh  # through the virtual server
```

| Case | Expected |
|---|---|
| No credentials | `401` |
| Wrong password | `403` |
| CSR CN naming another user | `403` |
| Correct credentials, matching CN | `200`, and the body must decode as PKCS#7 |

The last check matters for the same reason `estclient`'s exit code cannot be trusted: a `200` alone does not prove a certificate came back.

| Variable | Default | Effect |
|---|---|---|
| `EST_URL` | `http://127.0.0.1:8085` | Target. The shim directly isolates the gate from the BIG-IP; the virtual server exercises the whole path |
| `EST_USER` / `EST_PASS` | `client1.example.com` / `estlab123` | Credentials expected to succeed. The username is FQDN-shaped because CN matching compares it to the certificate name exactly |
| `EST_OTHER_USER` | `bob` | A different seeded user, used for the CN-mismatch case |
| `EST_DOMAIN` | `example.com` | Domain the requested CNs sit under; must satisfy the PKI role's `allowed_domains` |
| `CURL_OPTS` | `-sk` | Passed to every request. `-k` is there for the virtual server's lab certificate |
| `EST_PROXY_SECRET` | unset | Sent as `X-EST-Proxy-Secret`. Needed only for the shim-direct mode when the shim runs with a secret configured; through the virtual server the iRule supplies it |

Usernames are FQDN-shaped because the shim compares the CN to the username with exact equality while the CA independently requires the CN to sit inside `allowed_domains` — both hold only when the username *is* the certificate name. That interaction, and the Active Directory limits on it, are in [troubleshooting](../operations/troubleshooting.md#with-cn-enforcement-on-the-username-is-the-certificate-name).

Against Active Directory, pass the UPN prefix as the username and mind the password policy:

```bash
EST_URL=https://<vs-hostname>:8443 \
EST_USER=client1.f5lab.local EST_PASS='<complex-password>' \
EST_OTHER_USER=client2.f5lab.local \
CURL_OPTS="-sk --resolve <vs-hostname>:8443:<vs-ip>" \
./test-ldap-gate.sh
```
