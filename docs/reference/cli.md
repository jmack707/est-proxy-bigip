# CLI reference

## Overview

Six entry points. Run them in this order the first time:

| Script | Role |
|---|---|
| [`quickstart.sh`](#quickstartsh) | runs the four below end to end from one config file — start here |
| [`bootstrap-openbao-dev.sh`](#bootstrap-openbao-devsh) | stands up a throwaway PKI when no Vault/OpenBao exists |
| [`est_shim.py`](#est_shimpy) | the EST server itself |
| [`deploy_bigip.py`](#deploy_bigippy) | creates the BIG-IP pool, profile, iRule, and virtual server |
| [`install-cert-bigip.py`](#install-cert-bigippy) | installs a PEM cert/key pair you already hold |
| [`bigip-est-enroll.py`](#bigip-est-enrollpy) | obtains a certificate *for* a BIG-IP via a real EST exchange |

`bigip_lib.py` is a shared iControl REST helper module, not an entry point; it is imported by the two cert-installing scripts.

## `quickstart.sh`

Drives Parts 0–3 of the AD walkthrough non-interactively: dev-mode OpenBao, PKI bootstrap, shim configuration and start, BIG-IP object deploy, and the virtual server's own bootstrap certificate.

```bash
cp deploy.env.example deploy.env    # fill in every value
./quickstart.sh
```

Reads `deploy.env` from the working directory; takes no flags. Re-running is safe: the OpenBao bootstrap and the BIG-IP deploy are both idempotent, and the bootstrap certificate step re-issues a fresh certificate each run. Exits non-zero when a required variable is empty or when `VS_HOSTNAME` falls outside the PKI role's `allowed_domains`.

Still manual afterwards: AD/LDAPS setup, and testing with `estclient`.

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

Idempotent: existing objects are reported and skipped rather than erroring. Exit codes: `0` success, `1` validation or API failure with the reason on stderr.

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
