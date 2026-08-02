# Runbook: renew a BIG-IP certificate via EST

## When to use this

Last validated: 2026-07 — BIG-IP VE 21.1, OpenBao 2.2.0.

A certificate previously issued to a BIG-IP through this EST proxy is approaching expiry, or you are setting up the scheduled job that renews it. Uses RFC 7030 `simplereenroll`, which authenticates with the certificate being replaced.

Do not use this for a first issuance — use `enroll` mode ([CLI reference](../../reference/cli.md#bigip-est-enrollpy)). Do not use it for the EST virtual server's own certificate: renewing the certificate that serves EST through EST is the chicken-and-egg case, and that one is installed directly ([deploy](../../deploy.md#part-2--a-real-certificate-for-the-virtual-server)).

Do not use it once the certificate has already expired. `simplereenroll` presents it as TLS client identity, and an expired certificate will be rejected at the handshake, leaving no path but a fresh `enroll`.

## Prerequisites

| Need | Detail |
|---|---|
| Host | The workstation or job host with `estclient` installed — `apt install libest-utils` on Ubuntu 25.10+, otherwise `./estclient-docker.sh` and a container runtime ([ADR-0006](../../adr/0006-containerised-estclient-for-unpackaged-distros.md)) |
| Files | The current certificate and key PEMs from the previous run's `--save-dir` |
| Trust | CA chain PEM for `--est-cacert` |
| Access | BIG-IP account able to install `sys crypto` objects and modify the `client-ssl` profile |
| Reachability | EST virtual server on its listener port, and the BIG-IP management interface on 443 |
| Window | None normally. Attaching to a profile in production traffic is a configuration change — treat it as one |

## Procedure

1. Check what you are replacing, and confirm it has not already expired:

   ```bash
   openssl x509 -in ./saved/<object-name>.pem -noout -subject -enddate -fingerprint
   ```

2. Confirm the EST endpoint is healthy before relying on it. Check the output file, not the exit status — `estclient` returns `0` after a failed exchange ([troubleshooting](../troubleshooting.md#estclient-exits-0-on-a-failed-operation)), so a scheduled job that trusts `$?` will run on against a dead endpoint:

   ```bash
   export EST_OPENSSL_CACERT=/path/to/ca-chain.pem
   rm -rf /tmp/est-renew-check && mkdir -p /tmp/est-renew-check
   estclient -g -s <vs-hostname> -p 8443 -o /tmp/est-renew-check
   [ -s /tmp/est-renew-check/cacert-0-0.pkcs7 ] \
     || { echo "EST endpoint unhealthy; stopping" >&2; exit 1; }
   ```

3. Renew and install. `--attach-profile` is the step that changes what the device serves:

   ```bash
   python3 bigip-est-enroll.py renew \
     --est-host <vs-hostname> --est-port 8443 --est-cacert /path/to/ca-chain.pem \
     --existing-cert ./saved/<object-name>.pem \
     --existing-key ./saved/<object-name>.key \
     --bigip-host <bigip-mgmt-ip> --bigip-user admin --bigip-pass '<password>' \
     --cert-name <object-name> --attach-profile <clientssl-profile> \
     --save-dir ./saved
   ```

4. **Destructive step, inside the command above.** Installing over an existing object name detaches the profile, deletes the objects, installs the new pair, and reattaches. The profile briefly references `default.crt`/`default.key`, so clients connecting during that window see the stock certificate and strict ones will fail the hostname check. Keep a copy of the previous pair before running:

   ```bash
   cp ./saved/<object-name>.pem /tmp/prev-<object-name>.pem
   cp ./saved/<object-name>.key /tmp/prev-<object-name>.key
   ```

## Verification

The certificate must be genuinely new, not merely present — compare against what you captured in step 1:

```bash
tmsh list sys crypto cert <object-name>
openssl x509 -in ./saved/<object-name>.pem -noout -subject -enddate -fingerprint
tmsh list ltm profile client-ssl <clientssl-profile> cert-key-chain
```

Expected: a different fingerprint and a later expiry, the same subject, and the profile's `cert-key-chain` naming the new objects. Then confirm the device serves it:

```bash
openssl s_client -connect <bigip-vs>:443 -servername <bigip-fqdn> </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -enddate -fingerprint
```

## Rollback

1. Reinstall the previous pair from the copies made in step 4:

   ```bash
   python3 install-cert-bigip.py --bigip-host <bigip-mgmt-ip> --bigip-user admin \
     --bigip-pass '<password>' --cert-name <object-name> \
     --cert-file /tmp/prev-<object-name>.pem --key-file /tmp/prev-<object-name>.key \
     --attach-profile <clientssl-profile>
   ```

2. If the profile is left pointing at `default.crt` after a failure mid-sequence, that is the known interrupted state — reattach explicitly:

   ```bash
   tmsh modify ltm profile client-ssl <clientssl-profile> \
     cert-key-chain replace-all-with { <object-name> { cert <object-name>.crt key <object-name>.key } }
   ```

3. Re-run the verification commands. A rollback that leaves a mismatched pair shows up as `key(...) and certificate(...) do not match` — see [troubleshooting](../troubleshooting.md#reinstalling-a-cert-over-an-existing-name-breaks-the-certkey-pairing).

## Escalation

Capture before escalating: the output of every verification command above, `tmsh list sys crypto cert <object-name>` before and after, the full `bigip-est-enroll.py` output, the shim's log for the renewal window (`journalctl -u est-shim --since '10 min ago'` or `docker logs est-shim`), and the timestamps of each step.

Escalate to whoever owns the CA when the backend refused to sign — a `502` or a role rejection is a PKI matter, not a BIG-IP one. Escalate to the BIG-IP owner when the certificate was issued but the device will not serve it.

If the certificate expires while this is unresolved, stop trying to renew and switch to `enroll` mode with fresh credentials; `simplereenroll` cannot authenticate with an expired certificate.
