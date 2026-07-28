# Deploy

## Scope

Last validated: 2026-07 — BIG-IP VE 21.1, OpenBao 2.2.0.

Deploys the BIG-IP objects and, in the fast path, the whole stack: PKI backend, shim, BIG-IP pool/profile/iRule/virtual server, and the virtual server's own bootstrap certificate. Written for a lab or a UDF blueprint. Two parts remain manual in every path: directory setup ([AD/LDAPS setup](operations/ad-setup.md)), and testing with `estclient` per [Verification](#verification).

Not in scope: production hardening. TLS to the PKI backend is unverified ([ADR-0005](adr/0005-unverified-tls-to-the-pki-backend.md)) and the fast path uses dev-mode OpenBao ([ADR-0004](adr/0004-dev-mode-openbao-for-lab-bootstrap.md)).

## Prerequisites

The backend installed and verified ([install](install.md)), or the fast path below which installs it for you. Plus:

- BIG-IP reachable on 443 from wherever you run the scripts, with an account able to create LTM and `sys crypto` objects.
- A VLAN name for the virtual server, and a listener address on it.
- A hostname for the virtual server that resolves to that address on your test client, and that satisfies the PKI role's `allowed_domains`.
- For gated enrolment: LDAPS reachable from the backend host, plus a test user — on Active Directory this needs DC-side work first, covered in [AD/LDAPS setup](operations/ad-setup.md).

## Procedure

### Fast path

```bash
cp deploy.env.example deploy.env    # fill in every value
./quickstart.sh
```

This stands up dev-mode OpenBao, bootstraps its PKI, generates `est-shim.env` and starts the shim, deploys the BIG-IP objects, and issues and installs the virtual server's bootstrap certificate. Every variable is documented in the [configuration reference](reference/configuration.md#deployenv--quickstart-quickstartsh).

### Part 0 — a PKI backend where none exists

```bash
docker compose up -d openbao
BAO_ADDR=http://127.0.0.1:8200 ./bootstrap-openbao-dev.sh <your-domain>
# prints BAO_ROLE_ID / BAO_SECRET_ID / PKI_ROLE -- paste into est-shim.env
curl -s http://127.0.0.1:8200/v1/pki_int/ca_chain > ca-chain.pem
docker compose up -d est-shim
```

`ca-chain.pem` is what you set `EST_OPENSSL_CACERT` to when testing, and what you issue the virtual server's own certificate from. Pass `BAO_ADDR` explicitly: the script's own default port differs from the one compose publishes.

### Part 1 — BIG-IP objects

```bash
python3 deploy_bigip.py <bigip-mgmt-host> <user> <password> est_proxy.irule.tcl \
  --pool-member <backend-host>:8085 --vs-destination <vs-listener-ip>:8443 \
  --vs-vlan /Common/<your-vlan>
```

Creates the pool, a `client-ssl` profile with `peerCertMode: request`, the iRule, and the virtual server with SNAT automap. Everything environment-specific is a flag; object names are overridable if you want two instances side by side. Full flag table in the [CLI reference](reference/cli.md#deploy_bigippy).

### Part 2 — a real certificate for the virtual server

The virtual server needs a leaf certificate from a CA your EST clients trust, with CN or SAN matching the hostname they will connect to. F5's stock self-signed `default.crt` (`CN=localhost.localdomain`) cannot satisfy a strict client's hostname check no matter what is trusted.

```bash
python3 install-cert-bigip.py --bigip-host <bigip-mgmt-ip> --bigip-user admin \
  --bigip-pass '<password>' --cert-name <object-name> \
  --cert-file vs.crt --key-file vs.key --attach-profile est-clientssl
```

This certificate cannot come through EST: the virtual server is what serves EST.

### Part 3 — disable unclean shutdown on the profile

```bash
tmsh modify ltm profile client-ssl est-clientssl unclean-shutdown disabled
```

Required for strict clients. The reason is in [troubleshooting](operations/troubleshooting.md#unexpected-eof-while-reading-partway-through-a-response).

## Idempotency

| Object | Re-run behaviour |
|---|---|
| Pool, `client-ssl` profile, iRule, virtual server | Reported as already existing and skipped, not errored — confirmed by running `deploy_bigip.py` twice against a live BIG-IP and checking all four |
| OpenBao PKI mounts, roles, AppRole | Bootstrap tolerates existing mounts and re-applies configuration |
| Virtual server bootstrap certificate | Re-issued fresh on every `quickstart.sh` run, by design |
| `sys crypto cert`/`key` objects | Handled as detach → delete → install → reattach; an in-place overwrite is not reliable and can leave a genuinely mismatched pair |

## Verification

```bash
export EST_OPENSSL_CACERT=/path/to/ca-chain.pem
estclient -g -s <vs-hostname> -p 8443 -o /tmp/est-out                      # cacerts
estclient -e -s <vs-hostname> -p 8443 -o /tmp/est-out \
  --common-name <client-fqdn> -u <user> -h '<password>'                    # simpleenroll
```

Then verify the issued certificate against the chain:

```bash
base64 -d /tmp/est-out/cert-0-0.pkcs7 \
  | openssl pkcs7 -inform DER -print_certs > /tmp/est-out/cert.pem
openssl verify -CAfile ca-chain.pem /tmp/est-out/cert.pem
```

Also confirm the negative cases, since a gate that never refuses anything is not a gate: no credentials must give `401`, wrong password `403`, and a CSR whose CN does not match the authenticated user `403`. And `simplereenroll` with no client certificate must give `401` from the iRule, before the backend is reached.

## Rollback

1. Repoint the `client-ssl` profile at the stock certificate so the profile is never left referencing an object you are about to delete:

   ```bash
   tmsh modify ltm profile client-ssl est-clientssl \
     cert-key-chain replace-all-with { default { cert default.crt key default.key } }
   ```

2. Delete the objects the deploy created, in dependency order:

   ```bash
   tmsh delete ltm virtual est-proxy-vs
   tmsh delete ltm rule est_proxy
   tmsh delete ltm profile client-ssl est-clientssl
   tmsh delete ltm pool est-backend-pool
   ```

3. Stop the backend: `sudo systemctl stop est-shim`, or `docker compose down`.

4. Certificates already issued remain valid. Revoke them at the CA if that matters, and revoke the AppRole `secret_id`.
