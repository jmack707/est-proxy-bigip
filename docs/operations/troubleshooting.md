# Troubleshooting

Most entries below were found by running the stack against real infrastructure, or by reading a vendor's source, rather than by inspection. Where that is the case it is stated — it tells you the finding is verified rather than theorised.

## Symptom index

| Symptom (as seen) | Likely cause | Section |
|---|---|---|
| `Unhandled HTTP response` from `estclient` | Response used `HTTP/1.1` | [libest rejects the HTTP status line](#libest-rejects-the-http-status-line) |
| `unexpected eof while reading` | `client-ssl` profile skipping TLS `close_notify` | [unexpected eof while reading, partway through a response](#unexpected-eof-while-reading-partway-through-a-response) |
| `create_PKCS7` fails, no OpenSSL error queued | Base64 sent as one unbroken line | [PKCS#7 decode fails silently](#pkcs7-decode-fails-silently) |
| `EST_ERR_FQDN_MISMATCH` | Virtual server certificate CN/SAN does not match the connect hostname | [FQDN mismatch on the first connection](#fqdn-mismatch-on-the-first-connection) |
| `estclient -q` completes but the key will not load | `multipart/mixed` framing not accepted by the client | [serverkeygen key part does not parse in the client](#serverkeygen-key-part-does-not-parse-in-the-client) |
| `ValueError: Single '}' encountered in format string` | Bash mis-parsed a default containing `}` | [LDAP bind DN template arrives corrupted](#ldap-bind-dn-template-arrives-corrupted) |
| `key(...) and certificate(...) do not match` | Certificate installed over an existing object name | [reinstalling a cert over an existing name breaks the cert/key pairing](#reinstalling-a-cert-over-an-existing-name-breaks-the-certkey-pairing) |
| Containers vanish after the SSH session closes | Rootless Podman without lingering enabled | [rootless Podman containers die with the session](#rootless-podman-containers-die-with-the-session) |
| `401` on `simpleenroll` | No HTTP Basic credentials supplied | [401, 403, and which component refused](#401-403-and-which-component-refused) |
| `403 LDAP authentication failed` | Bad password, or a bind DN template that does not match the directory | [401, 403, and which component refused](#401-403-and-which-component-refused) |
| `403 CSR CN ... does not match authenticated LDAP user` | CN-match enforcement | [401, 403, and which component refused](#401-403-and-which-component-refused) |
| `502 OpenBao ...` | Shim cannot reach or authenticate to the PKI | [502 from the backend](#502-from-the-backend) |
| `common name ... not allowed by this role` | Requested CN outside the PKI role's `allowed_domains` | [CN refused by the PKI role](#cn-refused-by-the-pki-role) |

## libest rejects the HTTP status line

**Error text**

```text
Unhandled HTTP response
```

**Why it happens**

libest's HTTP status-line parser accepts only `HTTP/1.0`. A perfectly well-formed `HTTP/1.1` response fails immediately. Found by reading `src/est/est_client.c` in Cisco's libest, not by guessing.

**Confirm it is this**

```bash
curl -s -D - -o /dev/null http://<backend-host>:8085/.well-known/est/cacerts | head -1
```

Expected: `HTTP/1.0 200 OK`.

**Fix**

`est_shim.py` sets `protocol_version = "HTTP/1.0"`. If you see `HTTP/1.1`, something other than the shim answered — check the pool member and whether a proxy is in the path.

**Prevent recurrence**

Recorded in [ADR-0003](../adr/0003-http-1-0-and-wrapped-base64-for-libest.md), because the obvious "modernisation" reintroduces it.

## unexpected eof while reading, partway through a response

**Error text**

```text
unexpected eof while reading
```

**Why it happens**

A `client-ssl` profile's `unclean-shutdown` setting, enabled by default on `/Common/clientssl`, skips the TLS `close_notify` alert as a performance optimisation. `curl` tolerates a bare TCP FIN once it has read `Content-Length` bytes; libest's raw `SSL_read` loop does not, and aborts before finishing the body. This is why a working `curl` test proves less than it appears to.

**Confirm it is this**

```bash
tmsh list ltm profile client-ssl est-clientssl unclean-shutdown
```

**Fix**

```bash
tmsh modify ltm profile client-ssl est-clientssl unclean-shutdown disabled
```

Do this on your own profile, not the shared default.

**Prevent recurrence**

Part of [deploy](../deploy.md#part-3--disable-unclean-shutdown-on-the-profile). Test with `estclient`, not only `curl` — the two disagree here.

## PKCS#7 decode fails silently

**Error text**

```text
create_PKCS7 failed
```

with no OpenSSL error queued.

**Why it happens**

libest's base64 decode is a raw `BIO_f_base64()`, which requires periodic newlines — it is built for PEM-style wrapped base64. Python's `base64.b64encode()` produces a single unbroken line and breaks the decode with no diagnostic at all. Applies to `cacerts` and to enrolment responses.

**Confirm it is this**

```bash
curl -s http://<backend-host>:8085/.well-known/est/cacerts | head -3
```

Expected: several short lines, not one very long one.

**Fix**

`est_shim.py` uses `base64.encodebytes()`, which wraps at 76 characters.

**Prevent recurrence**

[ADR-0003](../adr/0003-http-1-0-and-wrapped-base64-for-libest.md).

## FQDN mismatch on the first connection

**Error text**

```text
EST_ERR_FQDN_MISMATCH
```

**Why it happens**

The virtual server needs a leaf certificate whose CN or SAN matches the hostname the client connects with, issued by a CA the client trusts — not merely a certificate whose issuer is trusted. libest's check is strict, and F5's stock `default.crt` (`CN=localhost.localdomain`) always fails it. Retrieving the chain through `cacerts` does not relax hostname verification.

**Confirm it is this**

```bash
openssl s_client -connect <vs-hostname>:8443 -servername <vs-hostname> </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -ext subjectAltName
```

**Fix**

Issue a certificate for the hostname clients use and install it ([deploy](../deploy.md#part-2--a-real-certificate-for-the-virtual-server)). Confirm the name resolves to the virtual server address on the client — `/etc/hosts` is enough in a lab.

**Prevent recurrence**

`quickstart.sh` issues the bootstrap certificate for `VS_HOSTNAME` and validates it against the PKI role up front.

## serverkeygen key part does not parse in the client

**Error text**

`estclient -q` completes the exchange, then fails converting the returned private key into an OpenSSL key object.

**Why it happens**

The `multipart/mixed` response is implemented and carries real data, but libest's `multipart_parser.c` does not fully accept the current framing. A known gap, not a misconfiguration.

**Confirm it is this**

```bash
curl -s -X POST -H 'Content-Type: application/pkcs10' --data-binary @csr.b64 \
  http://<backend-host>:8085/.well-known/est/serverkeygen | head -5
```

A `multipart/mixed` body with both parts present means the backend is behaving and the client is the limitation.

**Fix**

None available. `cacerts`, `simpleenroll`, and `simplereenroll` are unaffected; use `simpleenroll` with a client-generated key.

**Prevent recurrence**

Recorded as a non-goal in [architecture](../architecture.md#constraints-and-non-goals) so it is not rediscovered as a bug.

## LDAP bind DN template arrives corrupted

**Error text**

```text
ValueError: Single '}' encountered in format string
```

**Why it happens**

Bash's `${VAR:-default}` shorthand mis-parses when the default itself contains a literal `}` — as `{username}` does. The parser closes the expansion at the first `}` and leaves the second as a stray literal appended to the value. Confirmed empirically: `FOO="a"; echo "${FOO:-{x}}"` prints `a}`. This silently corrupted `LDAP_BIND_DN_TEMPLATE` and crashed the bind.

**Confirm it is this**

```bash
grep LDAP_BIND_DN_TEMPLATE /etc/est-shim/est-shim.env
```

Look for a trailing `}` that should not be there.

**Fix**

Correct the value. `quickstart.sh` uses an explicit emptiness test for this variable instead of the shorthand.

**Prevent recurrence**

Avoid `${VAR:-default}` wherever a default contains braces.

## reinstalling a cert over an existing name breaks the cert/key pairing

**Error text**

```text
profile ... key(...) and certificate(...) do not match
```

**Why it happens**

`tmsh install sys crypto cert/key <name> from-local-file` is not a reliable overwrite when the name already exists and is attached to a `client-ssl` profile. Re-running against the same name produced a genuinely broken pairing on the device, not just a warning.

**Confirm it is this**

```bash
tmsh list sys crypto cert <object-name>
tmsh list ltm profile client-ssl est-clientssl cert-key-chain
```

**Fix**

`bigip_lib.install_cert()` always detaches — repointing the profile at `default.crt`/`default.key` — then deletes, installs, and reattaches. To repair by hand, follow that same order.

**Prevent recurrence**

Use `install-cert-bigip.py` or `bigip-est-enroll.py` rather than `tmsh install` directly.

## rootless Podman containers die with the session

**Error text**

None. `docker compose up -d` looks successful, and the containers are gone by the next command.

**Why it happens**

Rootless Podman containers are tied to the user's session unless lingering is enabled.

**Confirm it is this**

```bash
loginctl show-user "$(id -un)" -p Linger
podman ps -a
```

**Fix**

```bash
loginctl enable-linger "$(id -un)"
```

**Prevent recurrence**

Set it on any host that will run `quickstart.sh` and then be left unattended, which is the point of `-d`. `quickstart.sh` checks and warns rather than leaving a stack that disappears silently. Docker does not have this behaviour.

## 401, 403, and which component refused

Read the response body: the iRule and the backend both return `401`, for different reasons.

| Response | Refused by | Meaning |
|---|---|---|
| `401 Client certificate required for reenroll` | iRule | `simplereenroll` with no certificate in the handshake; never reached the backend |
| `401` with `WWW-Authenticate: Basic realm="EST"` | backend | Enrolment is gated and no Basic credentials were sent |
| `401 reenroll requires client cert (X-SSL-Client-Cert missing)` | backend | Reached the shim without the injected header — usually a request that bypassed the virtual server |
| `403 LDAP authentication failed: ...` | backend | Bind failed. The detail distinguishes a wrong password from a template or connectivity problem |
| `403 CSR CN '...' does not match authenticated LDAP user '...'` | backend | CN-match enforcement |
| `405` with `Allow` | iRule | Wrong method for the operation |
| `400 Bad Content-Type for <op>` | iRule | Body was neither `application/pkcs10` nor `multipart/*` |

A bind failure that says `invalidCredentials` for a password you know is right usually means `LDAP_BIND_DN_TEMPLATE` does not match the directory's layout. Test the template independently:

```bash
ldapwhoami -x -H "$LDAP_URI" -D "<the-template-with-username-substituted>" -W
```

If a legitimate CN differs from the username by design — a service account enrolling device hostnames — set `LDAP_ENFORCE_CN_MATCH=false` deliberately, and understand that one credential can then request a certificate naming anything the PKI role allows.

## 502 from the backend

**Error text**

```text
502 OpenBao ca_chain error: ...
502 OpenBao AppRole login failed: ...
502 OpenBao sign error: ...
```

**Why it happens**

The shim is running and reachable, but the PKI backend is not: wrong `BAO_ADDR`, backend down, AppRole credentials wrong or expired, or a mount path that does not exist.

**Confirm it is this**

```bash
curl -s "$BAO_ADDR/v1/sys/health" ; echo
curl -s "$BAO_ADDR/v1/$PKI_MOUNT/ca_chain" | head -2
```

The second command needs no token, so it isolates reachability from authentication.

**Fix**

Correct `BAO_ADDR`, `PKI_MOUNT`, or the AppRole credentials, then restart the shim. After a dev-mode restart everything is gone — the mounts, the CA, and the AppRole — so re-run the bootstrap and paste the new credentials in.

**Prevent recurrence**

Dev mode being in-memory is the usual cause of a stack that worked yesterday. That is a property of the choice, not a fault ([ADR-0004](../adr/0004-dev-mode-openbao-for-lab-bootstrap.md)).

## CN refused by the PKI role

**Error text**

```text
common name ... not allowed by this role
```

**Why it happens**

The PKI signing role's `allowed_domains` does not cover the requested CN. The role created by the bootstrap allows the domain itself and its subdomains.

**Confirm it is this**

```bash
curl -s -H "X-Vault-Token: $BAO_TOKEN" "$BAO_ADDR/v1/$PKI_MOUNT/roles/$PKI_ROLE"
```

**Fix**

Request a CN inside the allowed domain, or widen the role. `quickstart.sh` checks `VS_HOSTNAME` against `DOMAIN` before deploying, so this fails early with a clear message instead of opaquely at issuance.
