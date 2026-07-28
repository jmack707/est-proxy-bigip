# Runbook: rotate the shim's AppRole credentials

## When to use this

Last validated: 2026-07 — BIG-IP VE 21.1, OpenBao 2.2.0.

Scheduled rotation of the PKI AppRole `secret_id` the shim authenticates with, or an unscheduled rotation after the credential may have been exposed — a leaked `est-shim.env`, a shared host, a departing administrator.

Do not use this to recover a dev-mode OpenBao after a restart. Dev mode is in-memory, so the mount, CA, role, and AppRole are all gone; re-run the bootstrap instead ([deploy](../../deploy.md#part-0--a-pki-backend-where-none-exists)).

The shim logs in per request, so it holds no long-lived session — a rotation takes effect on the next request after restart, with no drain step needed.

## Prerequisites

| Need | Detail |
|---|---|
| Access | A PKI backend token able to generate a new `secret_id` for the role, and revoke the old one |
| Access | Root or `sudo` on the backend host, or permission to recreate the container |
| Files | Write access to `/etc/est-shim/est-shim.env`, mode `0600` |
| Window | Brief. In-flight requests fail while the shim restarts |

## Procedure

1. Record which credential is in use, without printing the secret:

   ```bash
   sudo grep -c BAO_SECRET_ID /etc/est-shim/est-shim.env
   sudo grep BAO_ROLE_ID /etc/est-shim/est-shim.env
   ```

2. Confirm the current credential works, so you know a later failure is caused by the rotation:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' \
     http://<backend-host>:8085/.well-known/est/cacerts
   ```

   Expected: `200`.

3. Generate a new `secret_id` for the role at the PKI backend, using its own API or CLI, and keep the value out of shell history.

4. Update the environment file, preserving mode `0600`:

   ```bash
   sudo -e /etc/est-shim/est-shim.env
   ```

5. Restart the shim:

   ```bash
   sudo systemctl restart est-shim
   sudo systemctl show -p ExecMainStartTimestamp est-shim
   ```

   Container path: recreate it with the updated `--env-file`.

6. **Destructive step.** Revoke the old `secret_id` at the backend — only after verification below passes. Revoking first turns a routine rotation into an outage.

## Verification

```bash
curl -s http://<backend-host>:8085/.well-known/est/cacerts \
  | base64 -d | openssl pkcs7 -inform DER -print_certs -noout
```

Expected: the CA chain, unchanged. `cacerts` alone is not sufficient — it does not require a token at the backend — so also exercise a signing path, which does:

```bash
export EST_OPENSSL_CACERT=/path/to/ca-chain.pem
estclient -e -s <vs-hostname> -p 8443 -o /tmp/est-rotate \
  --common-name <client-fqdn> -u <user> -h '<password>'
```

A `502 OpenBao AppRole login failed` means the new credential is wrong or not yet active. Check the shim log:

```bash
journalctl -u est-shim --since '5 min ago'
```

## Rollback

1. Restore the previous `secret_id` in `/etc/est-shim/est-shim.env` and restart the shim. This only works if step 6 has not been done yet — which is why it is last.

   ```bash
   sudo -e /etc/est-shim/est-shim.env
   sudo systemctl restart est-shim
   ```

2. If the old credential is already revoked, generate another new `secret_id` and repeat from step 4. There is no path back to a revoked credential.

3. Confirm with the signing test above before closing out.

## Escalation

Capture before escalating: the shim log for the rotation window, the HTTP status codes from each verification command, the `role_id` in use (not the secret), and whether the old `secret_id` has been revoked — that single fact determines whether rollback is available.

Escalate to the PKI owner for anything involving role scope or credential generation, and note explicitly if the old credential is still valid, since an exposed credential that cannot yet be revoked is a live risk that needs a decision rather than a retry.
