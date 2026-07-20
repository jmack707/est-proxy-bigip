# est-proxy-bigip

Turn an F5 BIG-IP virtual server into an **RFC 7030 (EST) proxy** in front of
a HashiCorp Vault / OpenBao PKI backend — a working iRule, a minimal
OpenBao-backed EST server, and a deploy script, verified end-to-end with the
real Cisco `libest` client (`estclient`), not a curl approximation.

EST (Enrollment over Secure Transport) is the IETF standard for automated
certificate enrollment used by network devices, IoT, and PKI clients that
need something more structured than ACME. BIG-IP doesn't speak EST natively;
this project makes a VS behave like one by terminating/inspecting TLS at the
iRule layer and translating the RFC 7030 operations to a CA backend.

## What's here

- **`est_proxy.irule.tcl`** — the iRule. Enforces RFC 7030 method/
  content-type rules per operation (`cacerts`, `simpleenroll`,
  `simplereenroll`, `serverkeygen`, `csrattrs`, `fullcmc`), requires a client
  certificate for `simplereenroll` (returns `401` before the request reaches
  the backend if one isn't presented), forwards the client's TLS identity to
  the backend as `X-SSL-Client-*` headers, and supports routing by an
  optional EST `{label}` path segment to multiple backend CAs.
- **`est_shim.py`** — a small, dependency-free (stdlib only) Python EST
  server. Implements `cacerts`/`simpleenroll`/`simplereenroll`/`serverkeygen`
  by translating to a Vault/OpenBao PKI secrets engine's HTTP API
  (`issue`/`sign`/`ca_chain`). Runs behind the BIG-IP VS over plain HTTP —
  TLS is terminated at the VS.
- **`deploy_bigip.py`** — a one-off iControl REST script that creates the
  BIG-IP pool, a `client-ssl` profile (`peerCertMode: request`), the iRule,
  and the virtual server (SNAT automap).
- **`est-shim.service`** / **`est-shim.env.example`** — systemd unit + config
  template for running the shim as a backend service.

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

## Deploying

1. **Backend**: put `est_shim.py` somewhere reachable from the BIG-IP pool
   member network, configure `est-shim.env` (see `.env.example`) with an
   AppRole (or any Vault/OpenBao auth method you can adapt `bao_login()` to)
   scoped to:
   - `<pki_mount>/ca_chain` (unauthenticated `GET`, no policy needed) — used
     by `cacerts`.
   - `<pki_mount>/issue/<role>` (`update`) — used by `serverkeygen`.
   - `<pki_mount>/sign/<role>` (`update`) — used by `simpleenroll` /
     `simplereenroll` (signs the client's own CSR, unlike `issue` which
     generates a fresh keypair).

   Run it as `est-shim.service` (systemd unit provided).

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

## License

MIT — see `LICENSE`.
