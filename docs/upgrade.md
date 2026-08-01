# Upgrade

## Supported paths

Last validated: 2026-07 — BIG-IP VE 21.1, OpenBao 2.2.0.

| From | To | Method |
|---|---|---|
| Any commit of this repository | any later commit | Redeploy: the shim is a single file and the iRule is re-uploaded by `deploy_bigip.py`. There is no state to migrate |
| Dev-mode OpenBao | a real Vault or OpenBao cluster | Configuration only — repoint `BAO_ADDR`, supply an AppRole on the new backend, and reissue the virtual server's certificate from the new CA |
| BIG-IP VE 21.1 | later TMOS | Untested. The iRule uses `SSL::cert`, `X509::*`, and `HTTP::*` commands that have been stable for many releases, but treat it as unvalidated |

Not supported: keeping certificates issued by a dev-mode OpenBao after replacing it. Dev mode is in-memory, so the CA key is gone on restart and nothing it signed can be verified or renewed. Reissue from the new backend.

## Pre-upgrade checks

Capture, before touching anything:

```bash
# BIG-IP object state
tmsh list ltm virtual est-proxy-vs > /tmp/pre-vs.txt
tmsh list ltm rule est_proxy > /tmp/pre-irule.txt
tmsh list ltm profile client-ssl est-clientssl > /tmp/pre-profile.txt
tmsh list sys crypto cert <object-name> > /tmp/pre-cert.txt

# backend state
cp /etc/est-shim/est-shim.env /tmp/pre-est-shim.env   # contains a secret; delete after
systemctl show -p ActiveState est-shim
```

Note the current CA chain fingerprints, so you can tell afterwards whether clients need a new trust anchor:

```bash
openssl crl2pkcs7 -nocrl -certfile ca-chain.pem | openssl pkcs7 -print_certs -noout
```

Check which certificates are about to expire, so an upgrade does not coincide with a silent expiry:

```bash
tmsh list sys crypto cert | grep -E 'sys crypto cert|expiration'
```

## Procedure

1. Pull the new revision on the backend host and the operator workstation.

2. Replace the shim and restart it:

   ```bash
   sudo install -m 0755 est_shim.py /usr/local/bin/est_shim.py
   sudo systemctl restart est-shim
   ```

   Container path: `docker build -t est-shim . && docker rm -f est-shim && docker run -d --name est-shim -p 8085:8085 --env-file est-shim.env est-shim`.

3. Compare `est-shim.env.example` against your `est-shim.env` and add any new settings — the shim silently takes defaults for anything absent, so a new option will not announce itself:

   ```bash
   diff <(grep -oE '^[A-Z_]+' est-shim.env.example | sort -u) \
        <(grep -oE '^[A-Z_]+' /etc/est-shim/est-shim.env | sort -u)
   ```

4. Re-upload the iRule and reconcile the BIG-IP objects. The deploy is idempotent, so this updates the iRule and leaves existing objects alone:

   ```bash
   python3 deploy_bigip.py <bigip-mgmt-host> <user> <password> est_proxy.irule.tcl \
     --pool-member <backend-host>:8085 --vs-destination <vs-listener-ip>:8443 \
     --vs-vlan /Common/<your-vlan>
   ```

5. If the iRule's pool mapping changed, confirm `static::est_label_pools` still names pools that exist.

## Verification

```bash
export EST_OPENSSL_CACERT=/path/to/ca-chain.pem
estclient -g -s <vs-hostname> -p 8443 -o /tmp/post-upgrade     # cacerts
estclient -e -s <vs-hostname> -p 8443 -o /tmp/post-upgrade \
  --common-name <client-fqdn> -u <user> -h '<password>'        # simpleenroll
```

Confirm the shim actually restarted rather than continuing to serve from the old process, since a failed restart leaves the previous one running under `Restart=always`:

```bash
systemctl show -p ExecMainStartTimestamp est-shim
```

Re-run the negative cases from [deploy](deploy.md#verification). An upgrade that quietly turns a gate off is the failure mode worth checking for.

## Rollback

1. Reinstall the previous `est_shim.py` and restart:

   ```bash
   git checkout <previous-ref> -- est_shim.py
   sudo install -m 0755 est_shim.py /usr/local/bin/est_shim.py
   sudo systemctl restart est-shim
   ```

2. Restore the previous `est-shim.env` from `/tmp/pre-est-shim.env`, then delete that copy — it contains an AppRole secret.

3. Re-upload the previous iRule with `deploy_bigip.py` from the previous ref.

4. If a certificate was replaced, reattach the previous object:

   ```bash
   tmsh modify ltm profile client-ssl est-clientssl \
     cert-key-chain replace-all-with { <previous-name> { cert <previous-name>.crt key <previous-name>.key } }
   ```

5. Re-run the verification commands above before declaring the rollback done.

## Teardown

If the deployment came from `quickstart.sh`, `./teardown.sh --yes` reverses it from the same `deploy.env`, including the backend containers — see the [CLI reference](reference/cli.md#teardownsh). The manual sequence below is its equivalent, for a hand-built deployment or one whose object names differ from the defaults.

```bash
tmsh modify ltm profile client-ssl est-clientssl \
  cert-key-chain replace-all-with { default { cert default.crt key default.key } }
tmsh delete ltm virtual est-proxy-vs
tmsh delete ltm rule est_proxy
tmsh delete ltm profile client-ssl est-clientssl
tmsh delete ltm pool est-backend-pool
tmsh delete sys crypto cert <object-name>
tmsh delete sys crypto key <object-name>
```

Then remove the backend ([uninstall](install.md#uninstall)) and `docker compose down -v` for the lab PKI. On external systems: revoke the AppRole `secret_id`, revoke or let expire the certificates the CA issued, and remove the directory service account if one was created for enrolment.
