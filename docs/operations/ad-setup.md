# Active Directory / LDAPS setup

Directory-side prerequisites for gated enrolment (`LDAP_ENABLED=true`). Everything on this page happens on or against the domain controller, which is why no script in this repository can do it for you. FreeIPA ships with LDAPS enabled out of the box; Active Directory does not, and that gap is most of this page.

Last validated: 2026-07 — the LDAP gate itself verified against a real FreeIPA LDAPS bind; the Active Directory steps are from the original UDF walkthrough this page replaces.

## How authentication works here

An LDAP bind is literally just "attempt to log in": the shim sends a username and password to the directory, and the directory either accepts them or rejects them. A successful bind *is* the proof of identity — there is no separate password check, and no service account. The shim binds as the enrolling client's own credentials, templated through `LDAP_BIND_DN_TEMPLATE` ([configuration reference](../reference/configuration.md#est-shimenv--backend-est_shimpy)). For AD the simplest template is a UPN bind — `{username}@<your-domain>` — which needs no distinguished-name syntax at all.

The directory never sees or issues a certificate. It answers "is this a real, authenticated user?" and nothing more; the PKI backend signs whatever CSR the shim forwards once that answer is yes.

## Enable LDAPS on the domain controller

AD does **not** listen on 636 by default — it only starts once a valid TLS certificate is installed in the DC's local machine certificate store. If your domain already has AD Certificate Services issuing certificates to domain controllers via autoenrollment, you may already have this — skip to the verification below.

If not, the DC needs a certificate with:

- Subject/SAN matching the DC's FQDN (e.g. `dc01.example.com`)
- **Server Authentication** EKU (`1.3.6.1.5.5.7.3.1`)
- Installed into the **Local Computer → Personal** certificate store on the DC (not the current-user store)

Once that is present, AD DS picks it up automatically — no service restart required, though restarting `NTDS` (or the whole box, given a maintenance window) guarantees a clean pickup.

Verify LDAPS is actually listening, from any machine that can reach the DC:

```bash
openssl s_client -connect <dc-fqdn>:636 -showcerts </dev/null
```

Expected: a certificate chain prints and the connection succeeds — it is fine if `openssl` reports the certificate is not locally trusted, since this only confirms the port answers with TLS. If it hangs or refuses, LDAPS is not enabled yet and nothing downstream will work; fix this first.

## Get a test user

No special service account is needed — the shim binds as the enrolling user. Any existing AD account works for testing:

```powershell
New-ADUser -Name "est-test-user" -SamAccountName "est-test-user" `
  -UserPrincipalName "est-test-user@example.com" `
  -AccountPassword (ConvertTo-SecureString "SomeStrongPassword1!" -AsPlainText -Force) `
  -Enabled $true -PasswordNeverExpires $true
```

(`-PasswordNeverExpires` only keeps a test account from breaking mid-demo — do not do that for real accounts.)

Note the exact UPN (`est-test-user@example.com`): with the UPN bind template, that is the username clients authenticate with, and — when `LDAP_ENFORCE_CN_MATCH=true` — the CN their CSRs must carry.

## Verification

Run the gate check in [install](../install.md#verification) (no credentials must give `401`), plus the case that proves the shim can actually reach and bind to the directory:

```bash
curl -s -X POST http://<backend-host>:8085/.well-known/est/simpleenroll \
  -u "est-test-user:wrong-password" \
  -H 'Content-Type: application/pkcs10' --data-binary "" -o /dev/null -w '%{http_code}\n'
```

Expected: `403` — the directory was reached and refused the password. Anything else means the shim cannot reach or bind to the directory at all; check the shim's log for the underlying LDAP error before touching BIG-IP. Debugging the directory and the BIG-IP separately is much easier than debugging them together.

## The shim does not validate the directory's certificate

`est_shim.py` connects with `ldap3.Server(uri, use_ssl=True)` and no `Tls` object, so it will bind successfully even to an LDAPS endpoint presenting an untrusted or spoofed certificate. The bind still proves the password to *some* endpoint — but a network-level attacker who can interpose on the shim-to-DC path could harvest credentials. Acceptable in a lab; a real gap for production ([trust boundaries](../architecture.md#trust-boundaries)).

The fix, when it matters: pass an explicit `ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=...)` into the `Server()` call in `est_shim.py`, pointed at the CA that issued the DC's certificate.
