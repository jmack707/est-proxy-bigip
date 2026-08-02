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
| Gated enrolment refuses every CN a normal username could request | `LDAP_ENFORCE_CN_MATCH` and `allowed_domains` disagree | [with CN enforcement on, the username is the certificate name](#with-cn-enforcement-on-the-username-is-the-certificate-name) |
| A scripted `estclient` step "succeeds" but produces no certificate | `estclient` returns `0` after a failed exchange | [`estclient` exits 0 on a failed operation](#estclient-exits-0-on-a-failed-operation) |
| `E: Unable to locate package libest-utils` | Distribution older than Ubuntu 25.10 | [libest-utils is not packaged before Ubuntu 25.10](#libest-utils-is-not-packaged-before-ubuntu-2510) |
| `403 request did not arrive through the EST proxy` | `EST_PROXY_SECRET` and `--proxy-secret` disagree | [the proxy secret does not match](#the-proxy-secret-does-not-match) |
| `429 too many failed authentication attempts` | Rate limiter tripped | [authentication is throttled](#authentication-is-throttled) |
| `LDAP error: socket ssl wrapping error: certificate ...` | `LDAP_CA_FILE` set and `LDAP_URI` uses a name the certificate does not carry | [enabling LDAP verification breaks the bind](#enabling-ldap-verification-breaks-the-bind) |
| `ERROR: illegal certificate name` | `--cert-name`/`--attach-profile` contains characters outside a plain object name | [a certificate or profile name is refused](#a-certificate-or-profile-name-is-refused) |

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
502 OpenBao unreachable at <BAO_ADDR>: [Errno 111] Connection refused
```

**Why it happens**

The shim is running and reachable, but the PKI backend is not: wrong `BAO_ADDR`, backend down, AppRole credentials wrong or expired, or a mount path that does not exist.

The `error:` forms mean the backend answered and refused. The `unreachable at` form means nothing answered at all — the address is wrong or the backend is down. When the shim runs in a container, the most common cause is `BAO_ADDR=http://127.0.0.1:8200`, which points at the shim's own container rather than OpenBao; `bootstrap-openbao-dev.sh` prints that value because it is correct for a host-run shim. Use the compose service name (`http://openbao:8200`) instead — see the [configuration reference](../reference/configuration.md).

**Confirm it is this**

```bash
curl -s "$BAO_ADDR/v1/sys/health" ; echo
curl -s "$BAO_ADDR/v1/$PKI_MOUNT/ca_chain" | head -2
```

The second command needs no token, so it isolates reachability from authentication.

**Fix**

Correct `BAO_ADDR`, `PKI_MOUNT`, or the AppRole credentials, then restart the shim. A shim running in a container cannot reach the backend at `127.0.0.1` — that is the container itself; under compose use the service name (`http://openbao:8200`). After a dev-mode restart everything is gone — the mounts, the CA, and the AppRole — so re-run the bootstrap and paste the new credentials in.

**Prevent recurrence**

Dev mode being in-memory is the usual cause of a stack that worked yesterday. That is a property of the choice, not a fault ([ADR-0004](../adr/0004-dev-mode-openbao-for-lab-bootstrap.md)).

## With CN enforcement on, the username is the certificate name

**Error text**

Either of these, depending on which rule loses:

```text
403 CSR CN 'alice.example.com' does not match authenticated LDAP user 'alice'
502 OpenBao sign error: common name alice not allowed by this role
```

**Why it happens**

Two rules apply at once and they constrain each other:

- `est_shim.py` compares the CSR's CN to the authenticated username with **exact string equality** — `if cn != ldap_username`. Not a prefix, not the first label.
- The PKI role refuses any CN outside its `allowed_domains`.

Together these mean the directory username must itself be a name the role will sign. A user called `alice` can never enrol: CN `alice` matches the username but the CA refuses it, and CN `alice.example.com` satisfies the CA but not the username check.

**Confirm it is this**

```bash
grep -n -B4 'does not match authenticated' est_shim.py
curl -s -H "X-Vault-Token: $BAO_TOKEN" "$BAO_ADDR/v1/$PKI_MOUNT/roles/$PKI_ROLE" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["allowed_domains"])'
```

**Fix**

Name the login accounts after the certificates they will hold — `client1.example.com`, not `client1`. That is coherent for device enrolment, where the account *is* the device. Verified end to end against Active Directory and against the bundled fixture.

On Active Directory, **bind by `userPrincipalName` and let `sAMAccountName` be derived** — do not set `sAMAccountName` to the certificate name:

```powershell
New-ADUser -Name "est-client1" `
  -UserPrincipalName "client1.corp.example.com@corp.example.com" `
  -AccountPassword $pw -Enabled $true
```

The default `LDAP_BIND_DN_TEMPLATE` of `{username}@<domain>` binds by UPN, so `sAMAccountName` plays no part in the bind or in the CN comparison. It only has to be unique.

This matters because `sAMAccountName` is capped at 20 characters while a UPN prefix is not. Setting it to the certificate name imports a limit that otherwise does not apply — `client1.f5lab.local` is 19 and fits, but a longer host label or domain will not, and the rejection is

```text
The name provided is not a properly formed account name
```

which never mentions length and reads like a character-validity problem. Verified against a live domain controller: a **40-character** UPN prefix authenticated and enrolled cleanly end to end, while a 21-character `sAMAccountName` was refused outright.

One more Active Directory default to expect: **password complexity** requires three of four character classes, so a lab password like `estlab123` is refused at account creation and looks like a script bug rather than a policy rejection.

The alternative is `LDAP_ENFORCE_CN_MATCH=false`, which decouples the two rules — and means one valid credential can request a certificate naming anything the role allows. Choose it deliberately, not to make an error go away.

## A certificate or profile name is refused

**Error text**

```text
ERROR: illegal certificate name: 'x$(id)' -- only letters, digits, dot,
underscore and hyphen are allowed (max 128 chars)
```

**Why it happens**

`install-cert-bigip.py` and `bigip-est-enroll.py` build `tmsh` commands by interpolating the object name, and those commands run through `/mgmt/tm/util/bash` — a root shell on the device. A name outside `^[A-Za-z0-9._-]+$` is rejected up front, because such a name could otherwise execute commands on the BIG-IP: `util/bash` tokenises its argument rather than shell-splitting it, so `;` is inert but `$(...)` and backticks inside the command still evaluate.

**Confirm it is this**

The value you passed to `--cert-name` or `--attach-profile` contains something other than letters, digits, dot, underscore or hyphen — often a space, slash, or a shell metacharacter picked up from an upstream device inventory.

**Fix**

Use a plain BIG-IP object name. If the name is derived from a device identity in automation, sanitise it to the same character class before calling these tools — do not route around the check.

**Prevent recurrence**

[ADR-0008](../adr/0008-do-not-trust-proxy-supplied-identity-unconditionally.md), addendum. The guard lives in `bigip_lib.require_bigip_name`, so both cert tools share it.

## The proxy secret does not match

**Error text**

```text
403 request did not arrive through the EST proxy
```

**Why it happens**

The shim has `EST_PROXY_SECRET` set and the request did not carry a matching `X-EST-Proxy-Secret`. Either it reached the listener without going through the virtual server — which is the case this check exists to refuse — or the iRule was deployed without the value, or with a different one.

**Confirm it is this**

```bash
curl -sk -u admin:<password> \
  "https://<bigip-mgmt-ip>/mgmt/tm/ltm/rule/~Common~est_proxy" \
  | python3 -c 'import sys,json; print([l.strip() for l in json.load(sys.stdin)["apiAnonymous"].splitlines() if "est_proxy_secret" in l])'
```

An empty string in `set static::est_proxy_secret ""` means the iRule sends no header at all.

**Fix**

Redeploy the iRule with the value the shim expects. The two are set independently and must agree:

```bash
python3 deploy_bigip.py <bigip-mgmt-host> <user> <password> est_proxy.irule.tcl \
  --pool-member <backend-host>:8085 --vs-destination <vs-listener-ip>:8443 \
  --proxy-secret "<same value as EST_PROXY_SECRET>"
```

**Prevent recurrence**

[ADR-0008](../adr/0008-do-not-trust-proxy-supplied-identity-unconditionally.md). Testing the shim directly needs the secret too — `test-ldap-gate.sh` takes it as `EST_PROXY_SECRET`.

## Authentication is throttled

**Error text**

```text
429 too many failed authentication attempts; try again later
```

with a `Retry-After` header.

**Why it happens**

`AUTH_MAX_FAILURES` failed binds for that username inside `AUTH_WINDOW_SECONDS`. The counter is per username and per shim process, and a correct password during the window is refused too — that is what makes it a throttle rather than a hint.

**Confirm it is this**

```bash
docker logs est-shim 2>&1 | grep throttled
```

**Fix**

Wait out the window, or restart the shim to clear the counters in a lab. If a legitimate client trips it repeatedly, it is retrying with a stale password — fix the client rather than raising the limit.

**Prevent recurrence**

Because every gated enrolment is a live directory bind, the alternative to throttling here is lockout of the account in the real directory. The trade, including why the counter is not keyed on the client address, is in [ADR-0008](../adr/0008-do-not-trust-proxy-supplied-identity-unconditionally.md).

## Enabling LDAP verification breaks the bind

**Error text**

```text
403 LDAP authentication failed: LDAP error: socket ssl wrapping error:
certificate {'subject': ((('commonName', 'dc-f5lab.f5lab.local'),),) ...
```

**Why it happens**

Setting `LDAP_CA_FILE` turns on full verification, which includes checking the hostname in `LDAP_URI` against the directory certificate. Domain controller certificates issued by ADCS carry the DC's FQDN as a DNS SAN and no IP SAN, so an `LDAP_URI` pointing at an IP address fails — correctly. Before `LDAP_CA_FILE` existed the same URI worked, because nothing was verified.

**Confirm it is this**

```bash
openssl s_client -connect <dc-ip>:636 </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -ext subjectAltName
```

Compare those names with the host part of `LDAP_URI`.

**Fix**

Use a name the certificate carries, and make sure the shim can resolve it:

```bash
LDAP_URI=ldaps://dc-f5lab.f5lab.local:636
```

In a container add `--add-host <dc-fqdn>:<dc-ip>`, since the container does not inherit the host's `/etc/hosts`.

**Prevent recurrence**

Verified both ways during [ADR-0008](../adr/0008-do-not-trust-proxy-supplied-identity-unconditionally.md): the correct CA with the FQDN authenticates, and a deliberately wrong CA is refused.

## The shim does not verify the directory's TLS certificate

**Error text**

None. That is the problem — an invalid or substituted LDAPS certificate is accepted silently.

**Why it happens**

`est_shim.py` builds its connection as `ldap3.Server(LDAP_URI, use_ssl=...)` with no `Tls` object. `ldap3` defaults to `validate=ssl.CERT_NONE`, so `ldaps://` encrypts the session but does not authenticate the server. There is no configuration setting that turns verification on.

**Confirm it is this**

```bash
grep -n 'ldap3.Server' est_shim.py
```

A `Server(...)` call with no `tls=Tls(validate=...)` argument is this.

**Fix**

Set `LDAP_CA_FILE` to a bundle containing the directory's issuing CA. That switches `ldap3` to `CERT_REQUIRED`, which also verifies the hostname — so `LDAP_URI` must name the directory the way its certificate does, not by IP ([above](#enabling-ldap-verification-breaks-the-bind)).

Unset, the hop still needs a trusted network path, and directory passwords traverse it. That compounds with the cleartext BIG-IP-to-shim hop in [trust boundaries](../architecture.md#trust-boundaries): a foothold there yields real directory credentials, not just forged identity headers.

## `estclient` exits 0 on a failed operation

**Error text**

None from the shell. `estclient` prints the failure to stdout and still returns success:

```text
Get CA Cert failed with code 43 (EST_ERR_IP_CONNECT)
```

**Why it happens**

`estclient` validates its own invocation, but does not map an EST exchange failure onto its exit status. Confirmed empirically against `libest-utils` 3.2.0+ds-1.1 on Ubuntu 26.04, checking `$?` directly for each case:

| Failure | Exit |
|---|---|
| No arguments, or a required flag missing (`-g` with no `-s`) | `1` |
| CA chain named by `EST_OPENSSL_CACERT` does not exist | `1` |
| Connection refused or unreachable | **`0`** |
| Enrolment failed | **`0`** |
| Output directory not writable | **`0`** |

So anything that fails *after* the exchange starts looks like success to a shell, a `Makefile`, or a scheduled job. A failing run also writes nothing to the output directory, which is what makes the check below reliable.

**Confirm it is this**

```bash
estclient -g -s <vs-hostname> -p 8443 -o /tmp/est-check; echo "exit: $?"
ls -A /tmp/est-check | wc -l
```

An `exit: 0` with an empty directory is this.

**Fix**

Test for the artefact, not the exit status. Every verification in this repository already does — `openssl verify` on the issued certificate, and a decodable chain from `cacerts`. In a script:

```bash
rm -rf /tmp/est-check && mkdir -p /tmp/est-check
estclient -g -s <vs-hostname> -p 8443 -o /tmp/est-check
[ -s /tmp/est-check/cacert-0-0.pkcs7 ] || { echo "cacerts failed" >&2; exit 1; }
```

**Prevent recurrence**

Anything automating `estclient` — including `bigip-est-enroll.py` and the [certificate renewal runbook](runbooks/renew-bigip-certificate.md) — gates on output files rather than the return code.

## libest-utils is not packaged before Ubuntu 25.10

**Error text**

```text
E: Unable to locate package libest-utils
```

**Why it happens**

`libest-utils` first appears in Ubuntu 25.10 (questing), and is also in 26.04 LTS and 26.10, at `3.2.0+ds-1.1` in `universe`. No earlier release carries it in any component, so `add-apt-repository universe` changes nothing and the failure reads like a broken repository configuration.

**Confirm it is this**

```bash
. /etc/os-release && echo "$VERSION_ID"
apt-cache policy libest-utils
```

A `VERSION_ID` below `25.10`, with no candidate, is this.

**Fix**

Use the containerised client, which needs nothing on the host but a container runtime:

```bash
export EST_OPENSSL_CACERT=$PWD/ca-chain.pem
export ESTCLIENT_ADD_HOST=<vs-hostname>:<vs-listener-ip>
./estclient-docker.sh -g -s <vs-hostname> -p 8443 -o /out
```

**Prevent recurrence**

[ADR-0006](../adr/0006-containerised-estclient-for-unpackaged-distros.md). Building libest from source is the other option and is deliberately not the documented one: it needs a toolchain and `LD_LIBRARY_PATH` juggling, and yields a binary whose version nobody records.

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

The same rule applies to subject alternative names, with a different message:

```text
subject alternate name lab-ldap not allowed by this role
```

A short hostname is not inside `allowed_domains` even when the fully-qualified form is, so request FQDNs and reach the host by that name. This is why the [lab directory fixture](../reference/configuration.md#lab-directory-fixture-docker-composelab-ldapyml) carries a network alias of its full name rather than a short one.
