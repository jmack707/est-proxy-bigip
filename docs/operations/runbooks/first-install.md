# Runbook: first install onto a fresh environment

## When to use this

Last validated: 2026-07 — BIG-IP VE 21.1, OpenBao 2.2.0, `libest-utils` 3.2.0+ds-1.1, Ubuntu 22.04 and 26.04 hosts.

Standing the EST proxy up somewhere it has never run: a new lab, a fresh UDF blueprint, or a rebuilt BIG-IP. Takes you from an empty host to an issued certificate and a proven refusal path.

This is the ordered, mechanical form of [install](../../install.md) and [deploy](../../deploy.md). Use those when you need the reasoning; use this when you need the keystrokes.

Do not use it to change an existing deployment — re-running `quickstart.sh` is safe, but it re-issues the virtual server's certificate every time, which is churn you do not want against something already serving. For version moves see [upgrade](../../upgrade.md).

The result is **ungated**: `LDAP_ENABLED` defaults to `false`, so anyone who can route to the virtual server gets a certificate. Gating is a separate step and is not part of this procedure.

## Prerequisites

| Need | Detail |
|---|---|
| Backend host | Linux with Docker or Podman, `openssl`, and Python 3.9+. Must be reachable *from the BIG-IP* at a routable address — never `127.0.0.1` |
| BIG-IP | Management address, and an account able to create LTM objects and install `sys crypto` objects |
| Listener | A free IP on a BIG-IP VLAN, plus the VLAN name as `tmsh list net vlan` reports it |
| Naming | A DNS domain for issued certificates, and a service hostname **inside** that domain |
| Resolution | The service hostname must resolve to the listener address on whatever host runs the client |
| EST client | `libest-utils` (Ubuntu 25.10+), or a container runtime for [`estclient-docker.sh`](../../reference/cli.md#estclient-dockersh) |
| Window | None — nothing here modifies existing BIG-IP objects. The virtual server is new |

Worked example below uses backend `10.1.1.14`, BIG-IP `10.1.1.4`, listener `10.1.10.100:8443`, domain `f5lab.local`, hostname `est.f5lab.local`. Substitute your own.

## Procedure

1. Clone and confirm the toolchain:

   ```bash
   git clone <repo-url> est-proxy-bigip
   cd est-proxy-bigip
   docker --version && openssl version && python3 --version
   ```

2. Write the configuration. All eight values are required and `quickstart.sh` exits if any is empty:

   ```bash
   cp deploy.env.example deploy.env
   chmod 600 deploy.env
   ```

   ```bash
   DOMAIN=f5lab.local
   BACKEND_HOST=10.1.1.14
   BIGIP_HOST=10.1.1.4
   BIGIP_USER=admin
   BIGIP_PASS=<password>
   VS_DESTINATION=10.1.10.100:8443
   VS_VLAN=/Common/external
   VS_HOSTNAME=est.f5lab.local
   ```

   `VS_HOSTNAME` must fall inside `DOMAIN`, or the CA will refuse to sign it. Every variable is described in the [configuration reference](../../reference/configuration.md#deployenv--quickstart-quickstartsh).

3. Run the deploy:

   ```bash
   ./quickstart.sh
   ```

   It starts OpenBao, builds the CA, generates `est-shim.env`, starts the shim, creates the pool, `client-ssl` profile, iRule, and virtual server, then issues and attaches the virtual server's own certificate. Note the `ca-chain.pem` path it prints — everything downstream needs it.

4. On the BIG-IP, confirm you are on the device named by `BIGIP_HOST` before changing anything. An HA partner will report the profile as missing:

   ```bash
   tmsh list sys management-ip
   tmsh list ltm profile client-ssl est-clientssl unclean-shutdown
   ```

5. Disable unclean shutdown and persist the configuration:

   ```bash
   tmsh modify ltm profile client-ssl est-clientssl unclean-shutdown disabled
   tmsh save sys config
   ```

   Without the first, strict clients abort mid-response ([troubleshooting](../troubleshooting.md#unexpected-eof-while-reading-partway-through-a-response)). Without the second, everything created over iControl REST is lost at the next reboot.

6. Make the hostname resolve on the client host:

   ```bash
   getent hosts est.f5lab.local || \
     echo "10.1.10.100 est.f5lab.local" | sudo tee -a /etc/hosts
   ```

7. Install an EST client. Native package first:

   ```bash
   sudo apt install libest-utils
   ```

   On releases older than Ubuntu 25.10 that package does not exist ([troubleshooting](../troubleshooting.md#libest-utils-is-not-packaged-before-ubuntu-2510)); use the container, which needs no host `/etc/hosts` entry but does need `ESTCLIENT_ADD_HOST`:

   ```bash
   export ESTCLIENT_ADD_HOST=est.f5lab.local:10.1.10.100
   ./estclient-docker.sh -g -s est.f5lab.local -p 8443 -o /out
   ```

## Verification

Run these in order. Each adds one layer, so the first failure names the layer at fault.

1. Backend and CA, with no BIG-IP in the path:

   ```bash
   curl -s http://10.1.1.14:8085/.well-known/est/cacerts \
     | base64 -d | openssl pkcs7 -inform DER -print_certs -noout
   ```

   Expected: subject and issuer lines for your root and intermediate. A `502` is [the backend](../troubleshooting.md#502-from-the-backend); an empty reply means the shim is not listening.

2. The same request through the virtual server, and the certificate it presents:

   ```bash
   curl -sk https://10.1.10.100:8443/.well-known/est/cacerts | head -3
   openssl s_client -connect 10.1.10.100:8443 -servername est.f5lab.local </dev/null 2>/dev/null \
     | openssl x509 -noout -subject -issuer -ext subjectAltName
   ```

   Expected: several short base64 lines, then a subject and SAN of `est.f5lab.local` issued by your intermediate. A mismatch here is [FQDN mismatch](../troubleshooting.md#fqdn-mismatch-on-the-first-connection) waiting to happen.

3. `cacerts` with the real client. Assert on the output file — `estclient` returns `0` even when the exchange fails ([troubleshooting](../troubleshooting.md#estclient-exits-0-on-a-failed-operation)):

   ```bash
   rm -rf /tmp/est-out && mkdir -p /tmp/est-out
   estclient -g -s est.f5lab.local -p 8443 -o /tmp/est-out
   [ -s /tmp/est-out/cacert-0-0.pkcs7 ] || { echo "cacerts failed" >&2; exit 1; }
   ```

4. Enrol a certificate and verify it against the chain. Generate the key yourself so it is available for a later renewal:

   ```bash
   openssl genrsa -out /tmp/est-out/client1.key 2048
   estclient -e -s est.f5lab.local -p 8443 -o /tmp/est-out \
     -x /tmp/est-out/client1.key --common-name client1.f5lab.local
   base64 -d /tmp/est-out/cert-0-0.pkcs7 \
     | openssl pkcs7 -inform DER -print_certs > /tmp/est-out/client1.pem
   openssl verify -CAfile ca-chain.pem /tmp/est-out/client1.pem
   ```

   Expected: `OK`, with a subject of `CN = client1.f5lab.local`. A CN outside `DOMAIN` is [refused by the role](../troubleshooting.md#cn-refused-by-the-pki-role).

5. Confirm the iRule refuses what it should. An install is not finished until these three have been seen:

   ```bash
   curl -sk -o /dev/null -w 'reenroll no-cert: %{http_code}\n' -X POST \
     -H 'Content-Type: application/pkcs10' --data-binary '' \
     https://10.1.10.100:8443/.well-known/est/simplereenroll
   curl -sk -o /dev/null -w 'GET on enroll:    %{http_code}\n' \
     https://10.1.10.100:8443/.well-known/est/simpleenroll
   curl -sk -o /dev/null -w 'bad content-type: %{http_code}\n' -X POST \
     -H 'Content-Type: text/plain' --data-binary '' \
     https://10.1.10.100:8443/.well-known/est/simpleenroll
   ```

   Expected: `401`, `405`, `400`. Anything reaching the backend instead means the iRule is not matching.

## Rollback

Nothing here modifies pre-existing objects, so rollback is removal. Repoint the profile before deleting anything it references:

```bash
tmsh modify ltm profile client-ssl est-clientssl \
  cert-key-chain replace-all-with { default { cert default.crt key default.key } }

tmsh delete ltm virtual est-proxy-vs
tmsh delete ltm rule est_proxy
tmsh delete ltm profile client-ssl est-clientssl
tmsh delete ltm pool est-backend-pool
tmsh save sys config
```

Then stop the backend on the shim host:

```bash
docker compose down
```

Certificates already issued stay valid — revoke them at the CA if that matters, and revoke the AppRole `secret_id`. Full detail in [deploy](../../deploy.md#rollback).

## Escalation

Capture before escalating: the complete `quickstart.sh` output, the HTTP status from every verification command, `tmsh list ltm virtual est-proxy-vs` and `tmsh list ltm pool est-backend-pool` from the BIG-IP named by `BIGIP_HOST`, the shim log (`docker logs est-shim`), and which verification step first failed.

Escalate to the CA owner when the backend refused to sign — a `502` or a role rejection is a PKI matter. Escalate to the BIG-IP owner when the virtual server is down or the profile will not take the certificate. If verification step 1 passed and step 2 failed, the fault is on the BIG-IP and not in the backend, which is the single most useful thing to say in the handover.

If the stack worked and then stopped, check whether the OpenBao container restarted before anything else: dev mode is in-memory, so a restart discards the CA, the mounts, and the AppRole, and the fix is to re-run `quickstart.sh` rather than to debug.
