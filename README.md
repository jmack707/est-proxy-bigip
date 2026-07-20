# est-proxy-bigip

Turn an F5 BIG-IP virtual server into an **RFC 7030 (EST) proxy** in front of
a HashiCorp Vault / OpenBao PKI backend, with optional **FreeIPA or Active
Directory** client authentication — a working iRule, a minimal, portable
(containerized) EST server, and a deploy script, verified end-to-end with
the real Cisco `libest` client (`estclient`), not a curl approximation.

EST (Enrollment over Secure Transport) is the IETF standard for automated
certificate enrollment used by network devices, IoT, and PKI clients that
need something more structured than ACME. BIG-IP doesn't speak EST natively;
this project makes a VS behave like one by terminating/inspecting TLS at the
iRule layer and translating the RFC 7030 operations to a CA backend.

## EST overview (RFC 7030)

EST is the IETF's HTTPS-based certificate enrollment protocol — think of it
as ACME's older, more enterprise/PKI-flavored sibling. It's what a lot of
network gear (routers, switches, IoT, 802.1X supplicants) uses to get and
renew certificates from a CA automatically, without a human copying files
around. Everything below maps directly onto a piece of this project.

**Trust model.** Before a client can talk to an EST server it needs some way
to trust it — RFC 7030 calls this the "explicit TA database" bootstrap
problem. Two ways to solve it:
- *Implicit trust anchor*: the client already has a CA cert it trusts (e.g.
  baked in at manufacture time, or from a previous enrollment) and uses it
  to verify the EST server's TLS cert directly.
- *Explicit trust anchor via `/cacerts`*: the client connects with **some**
  minimal trust (often just "accept anything for this one bootstrap call"),
  fetches the CA's cert chain over `GET /cacerts`, and then re-verifies
  everything from that point on using the freshly-retrieved chain.

  This project's [`est_shim.py`](est_shim.py) implements `/cacerts` by
  proxying to the CA backend's `ca_chain` endpoint; see gotcha #4 below for
  why the *server's* leaf cert also has to satisfy a real hostname check —
  fetching the CA chain doesn't relax that.

**Core operations** (all under `/.well-known/est/`, optionally
`/.well-known/est/{label}/...` — see "labels" below):

| Operation | Method | Purpose | Client auth |
|---|---|---|---|
| `cacerts` | GET | Fetch the CA's cert chain (bootstrap trust, or just refresh it) | none |
| `csrattrs` | GET | Server tells the client which CSR attributes it wants included (optional, rarely mandatory in practice) | none |
| `simpleenroll` | POST | Client generates its own keypair + CSR, submits the CSR, gets back a signed cert | none required by the spec (may be gated by TLS client cert, HTTP auth, or a `challengePassword` in the CSR, depending on the deployment — this project gates it with HTTP Basic auth against FreeIPA/AD, see "LDAP authentication" below) |
| `simplereenroll` | POST | Same as `simpleenroll`, but for renewing a cert the client **already holds** | **mandatory**: the client authenticates the TLS session with its current (possibly expiring) cert |
| `serverkeygen` | POST | Client asks the *server* to generate the keypair too (not just sign a CSR) — useful for constrained devices that can't do keygen cheaply | same as simpleenroll |
| `fullcmc` | POST | Full CMC (RFC 5272) request/response instead of the simplified PKCS#10/PKCS#7 flow — used when you need CMC's richer semantics (multi-cert requests, POP linking, etc.) | varies |

  [`est_proxy.irule.tcl`](est_proxy.irule.tcl) enforces the method +
  content-type rules from this table per-operation, and specifically
  enforces the `simplereenroll` client-auth requirement by checking
  `SSL::cert count` and returning `401` before the request ever reaches the
  backend if no client cert was presented during the TLS handshake — this is
  the one operation the protocol says the *server* must not skip
  authentication on. `est_shim.py` implements `cacerts` / `simpleenroll` /
  `simplereenroll` / `serverkeygen`; `csrattrs` returns an empty response
  (technically valid — it's optional) and `fullcmc` isn't implemented (CMC
  is a materially different encoding, out of scope here).

**HTTP transport encoding.** EST bodies aren't sent as raw binary or JSON —
they use MIME types borrowed from S/MIME:
- CSRs (`simpleenroll`/`simplereenroll`/`serverkeygen` requests):
  `Content-Type: application/pkcs10`, body = base64-encoded DER PKCS#10.
- Certs and CA chains (`cacerts` responses, successful enroll/reenroll
  responses): `Content-Type: application/pkcs7-mime; smime-type=certs-only`,
  body = base64-encoded **degenerate PKCS#7** — a `SignedData` structure
  with no actual signature, just a `certificates` field used as a container
  to carry one or more X.509 certs. (`est_shim.py`'s `pkcs7_degenerate()`
  shells out to `openssl crl2pkcs7 -nocrl` to build this — `cryptography`'s
  Python bindings can't produce a degenerate PKCS#7 directly.)
- `serverkeygen` responses need to carry back **both** a cert and a private
  key, so they're `multipart/mixed`: one part `application/pkcs7-mime`
  (the cert), one part `application/pkcs8` (the key) — see gotcha #5 for the
  current limitation there.
- Per spec, all of the above should also carry `Content-Transfer-Encoding:
  base64`. See gotcha #3 for a real-world parser quirk this triggers.

**Labels / multiple CAs.** RFC 7030 allows an optional path segment —
`/.well-known/est/{label}/simpleenroll` instead of
`/.well-known/est/simpleenroll` — so one EST server (or, here, one BIG-IP
VS) can front multiple distinct CAs/policies by label. `est_proxy.irule.tcl`
parses this segment and looks it up in a `static::est_label_pools` array,
routing each label to a different backend pool; `est_shim.py` also parses it
(currently informational — extending it to select a different PKI mount/role
per label on the backend side is a natural next step, not yet wired up).

**Adjacent-but-out-of-scope specs**, for context if you go looking: **EST-coaps**
(RFC 9148) adapts the same operations to CoAP for very constrained
IoT devices instead of HTTPS; **BRSKI** (RFC 8995) builds a zero-touch
bootstrapping/ownership-voucher scheme on top of EST for automated device
onboarding. Neither is implemented here — this project sticks to plain
HTTPS EST.

## LDAP authentication (FreeIPA / Active Directory)

RFC 7030 deliberately leaves `simpleenroll` client authentication up to the
deployment — it's the CA operator's job to decide who's allowed to enroll,
not the protocol's. In most real environments that's a directory service,
not a bearer token, so `est_shim.py` can gate `simpleenroll` on an **LDAP
bind** against FreeIPA or Active Directory before it ever talks to the CA
backend:

1. The EST client sends the request with **HTTP Basic auth**
   (`estclient -u <user> -h <password> ...` — this maps directly onto
   libest's built-in support for exactly this).
2. The shim binds to the directory as that user (`LDAP_BIND_DN_TEMPLATE`,
   substituting `{username}`) — a **successful bind is treated as
   authentication**, no separate password check needed, since the bind
   itself proves the password.
3. **CN-match enforcement** (`LDAP_ENFORCE_CN_MATCH`, on by default): the
   shim also checks that the CSR's `CN` matches the authenticated username,
   so a valid credential for user A can't be used to mint a cert claiming to
   be user B.
4. `simplereenroll` is deliberately **not** gated by LDAP — it already has
   its own authentication (the client's existing TLS cert, enforced by the
   iRule), and RFC 7030 treats that as sufficient. Layering LDAP on top
   would just mean checking the same identity twice through two different
   mechanisms.

Configuration is a bind-DN template, so the same code works against either
directory — see `est-shim.env.example` for concrete FreeIPA and AD examples.
No new BIG-IP-side configuration is needed: the iRule already passes the
`Authorization` header straight through to the pool (it only touches
`X-SSL-Client-*`).

This was validated against a real FreeIPA server (LDAPS bind, not a mock):
unauthenticated request → `401`; wrong password → `403`; CSR CN not matching
the authenticated user → `403`; correct credentials + matching CN → a real
certificate issued by the CA. The one non-stdlib dependency this adds is
[`ldap3`](https://ldap3.readthedocs.io/) (pure Python, no `libldap`/`libsasl`
system packages required) — see `requirements.txt`.

## What's here

- **`est_proxy.irule.tcl`** — the iRule. Enforces RFC 7030 method/
  content-type rules per operation (`cacerts`, `simpleenroll`,
  `simplereenroll`, `serverkeygen`, `csrattrs`, `fullcmc`), requires a client
  certificate for `simplereenroll` (returns `401` before the request reaches
  the backend if one isn't presented), forwards the client's TLS identity to
  the backend as `X-SSL-Client-*` headers, and supports routing by an
  optional EST `{label}` path segment to multiple backend CAs.
- **`est_shim.py`** — a small Python EST server (one optional dependency:
  `ldap3`, only needed if `LDAP_ENABLED=true`). Implements `cacerts`/
  `simpleenroll`/`simplereenroll`/`serverkeygen` by translating to a
  Vault/OpenBao PKI secrets engine's HTTP API (`issue`/`sign`/`ca_chain`),
  with optional FreeIPA/AD client authentication (see below). Runs behind
  the BIG-IP VS over plain HTTP — TLS is terminated at the VS.
- **`deploy_bigip.py`** — a one-off iControl REST script that creates the
  BIG-IP pool, a `client-ssl` profile (`peerCertMode: request`), the iRule,
  and the virtual server (SNAT automap).
- **`est-shim.service`** / **`est-shim.env.example`** — systemd unit + config
  template for running the shim as a plain host process.
- **`Dockerfile`** / **`requirements.txt`** — for running the shim as a
  container instead (same env-var configuration either way).
- **`docker-compose.yml`** / **`bootstrap-openbao-dev.sh`** — for
  environments with no existing Vault/OpenBao instance (e.g. F5 UDF): brings
  up a throwaway dev-mode OpenBao and wires up its PKI + an AppRole for the
  shim in one script. See "No existing Vault/OpenBao? (e.g. F5 UDF)" below.

## Topology

```
EST client --> BIG-IP virtual server (TLS)
               client-ssl profile: peerCertMode=request,
               cert issued by your CA, CN/SAN = the VS hostname
            -> iRule est_proxy (method/content-type checks,
               client-cert enforcement for reenroll,
               X-SSL-Client-* header injection)
            -> pool -> est_shim.py (plain HTTP)
            -> Vault/OpenBao PKI secrets engine (issue/sign/ca_chain)
```

## No existing Vault/OpenBao? (e.g. F5 UDF)

`est_shim.py` talks to any Vault-API-compatible PKI backend — it doesn't
care whether that's a real production Vault cluster or a five-second
throwaway instance. If you're building this in an environment like F5 UDF
that doesn't already have one, `bootstrap-openbao-dev.sh` stands up a
**dev-mode** OpenBao (in-memory, auto-unsealed, no init/unseal ceremony) and
wires up a root CA, intermediate CA, signing role, and a scoped AppRole for
the shim — everything `est_shim.py` needs — in one shot. Every command in
it was validated against a real OpenBao 2.2.0 instance before being
committed here, not copied from docs and assumed correct.

**This is a lab/demo-only choice, not a production one** — dev mode stores
everything in memory (nothing survives a restart) and starts with a single,
known root token. Fine for a UDF blueprint that gets torn down and rebuilt
regularly; not fine for anything that needs to persist or be trusted beyond
that.

```sh
docker compose up -d openbao          # or: podman run -d --network host \
                                       #     -e BAO_DEV_ROOT_TOKEN_ID=root \
                                       #     ghcr.io/openbao/openbao:2.2.0 server -dev

BAO_ADDR=http://127.0.0.1:8200 ./bootstrap-openbao-dev.sh your-domain.com
# prints BAO_ROLE_ID / BAO_SECRET_ID / PKI_ROLE -- paste them into est-shim.env

curl -s http://127.0.0.1:8200/v1/pki_int/ca_chain > ca-chain.pem
# this is your EST_OPENSSL_CACERT for testing, and what you issue the VS's
# own certificate from (see "Deploying" step 3 below)

docker compose up -d est-shim
```

## Deploying

1. **Backend**: run `est_shim.py` somewhere reachable from the BIG-IP pool
   member network, configured (see `est-shim.env.example`) with a Vault/
   OpenBao AppRole (or any auth method you can adapt `bao_login()` to)
   scoped to:
   - `<pki_mount>/ca_chain` (unauthenticated `GET`, no policy needed) — used
     by `cacerts`.
   - `<pki_mount>/issue/<role>` (`update`) — used by `serverkeygen`.
   - `<pki_mount>/sign/<role>` (`update`) — used by `simpleenroll` /
     `simplereenroll` (signs the client's own CSR, unlike `issue` which
     generates a fresh keypair).

   and, if you want FreeIPA/AD-gated enrollment, `LDAP_ENABLED=true` +
   `LDAP_URI`/`LDAP_BIND_DN_TEMPLATE` (see "LDAP authentication" above).

   Two ways to run it:
   ```sh
   # plain host process
   pip install -r requirements.txt   # only strictly needed if LDAP_ENABLED
   cp est-shim.env.example /etc/est-shim/est-shim.env   # edit it, then:
   sudo cp est-shim.service /etc/systemd/system/
   sudo systemctl enable --now est-shim

   # or as a container
   docker build -t est-shim .
   docker run -d --name est-shim -p 8085:8085 --env-file est-shim.env est-shim
   ```

2. **BIG-IP objects**:

   ```sh
   python3 deploy_bigip.py <bigip-mgmt-host> <user> <password> est_proxy.irule.tcl
   ```

   This is idempotent — objects that already exist are reported and skipped,
   not errored on. Edit the pool member address/port and VS
   destination/VLAN at the top of `deploy_bigip.py` for your environment
   before running (defaults are placeholders for a lab network).

3. **Give the VS a real leaf certificate from a CA your EST client will
   trust**, with a CN/SAN matching the hostname you'll connect with. F5's
   stock self-signed `default.crt` will fail most strict EST clients'
   hostname verification (see gotcha #4 below) — a self-signed cert with a
   mismatched CN can never satisfy a hostname check regardless of what's
   trusted.

## Testing with a real EST client (`estclient`, Debian package `libest-utils`)

```sh
# EST's bootstrap trust problem: point the client at a CA chain it can pin
# for the very first connection.
export EST_OPENSSL_CACERT=/path/to/your-ca-chain.pem

# GET /cacerts
estclient -g -s <vs-hostname> -p 8443 -o /tmp/est-out

# POST /simpleenroll
estclient -e -s <vs-hostname> -p 8443 -o /tmp/est-out \
  --common-name test-client.example.com

# POST /simplereenroll (needs the enrolled cert as TLS client identity;
# estclient -e writes response bodies as base64 text under names like
# cert-0-0.pkcs7 -- decode with:
#   base64 -d cert-0-0.pkcs7 | openssl pkcs7 -inform DER -print_certs > cert.pem
estclient -r -s <vs-hostname> -p 8443 -o /tmp/est-out \
  -c /tmp/est-out/cert.pem -k /tmp/est-out/key-x-x.pem
```

## Gotchas found the hard way

Found by pulling `src/est/est_client.c` from
[`cisco/libest`](https://github.com/cisco/libest) and reading the actual
parsing code rather than guessing:

1. **libest's HTTP status-line parser only accepts `HTTP/1.0`.** A perfectly
   well-formed `HTTP/1.1` response makes it fail immediately with "Unhandled
   HTTP response". `est_shim.py` sets `protocol_version = "HTTP/1.0"`.
2. **A BIG-IP client-ssl profile's `unclean-shutdown` setting (enabled by
   default on `/Common/clientssl`) skips the TLS `close_notify` alert** as a
   performance optimization. `curl` tolerates a bare TCP FIN once it's read
   `Content-Length` bytes; libest's raw `SSL_read` loop does not, and aborts
   with `unexpected eof while reading` before finishing the response body.
   Fix: `tmsh modify ltm profile client-ssl <your-profile> unclean-shutdown
   disabled` — do this on your own profile, not the shared default.
3. **libest's base64 decode (`b64_decode_cacerts`, also used for enrollment
   responses) is a raw `BIO_f_base64()`, which requires periodic newlines**
   — it's built for PEM-style wrapped base64, not one unbroken line.
   Python's `base64.b64encode()` produces a single line and silently breaks
   the decode (`create_PKCS7` fails with no OpenSSL error queued at all).
   Fixed by using `base64.encodebytes()` (76-char MIME wrapping) instead.
4. **The VS needs a leaf cert with a CN/SAN matching the hostname the client
   connects with**, issued by a CA the client trusts — not just "any cert
   the client happens to trust the issuer of." libest's `EST_ERR_FQDN_MISMATCH`
   check is strict, and F5's stock `default.crt` (`CN=localhost.localdomain`)
   will always fail it.
5. **Known gap**: `serverkeygen`'s `multipart/mixed` response is implemented
   and returns real data, but libest's `multipart_parser.c` doesn't fully
   accept the current framing (`estclient -q` completes the EST exchange but
   fails parsing the returned private key back into an OpenSSL key object).
   `cacerts`/`simpleenroll`/`simplereenroll` are unaffected.

## Status

Verified end-to-end with the real `estclient` (libest) against a live F5
BIG-IP VE 21.1 virtual server and a HashiCorp Vault-API-compatible PKI
backend (OpenBao 2.2.0):

- `cacerts` — decoded CA chain matches the configured root + intermediate.
- `simpleenroll` — issued certificate verifies clean against the CA chain
  (`openssl verify`).
- `simplereenroll` — accepts the simpleenroll-issued cert as TLS client
  identity, returns a fresh certificate (new serial/validity), also verifies
  clean. Rejecting a `simplereenroll` with no client cert presented (`401`
  from the iRule, before the backend is ever reached) is confirmed too.
- `serverkeygen` — backend logic works (verified with curl); the real EST
  client's multipart parsing needs more work (see gotcha #5).
- **`bootstrap-openbao-dev.sh`** — every command run end-to-end against a
  fresh dev-mode OpenBao 2.2.0 instance: root CA generated, intermediate CA
  generated + signed + set, signing role created, AppRole created, and the
  resulting credentials handed to a live `est_shim.py` which successfully
  served `cacerts` and `simpleenroll` (`openssl verify` clean) using them.
- **LDAP authentication** — validated against a real FreeIPA server (LDAPS
  bind, not a mock): unauthenticated `simpleenroll` → `401`; wrong
  credentials → `403`; correct credentials but CSR CN not matching the
  authenticated user → `403`; correct credentials + matching CN → real
  certificate issued. Also validated running the shim as a container
  (`docker build` / `podman build`), not just as a host process.

## License

MIT — see `LICENSE`.
