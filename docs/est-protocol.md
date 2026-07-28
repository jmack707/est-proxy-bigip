# EST protocol notes (RFC 7030)

Background for reading the rest of the documentation. Every item here maps onto a piece of this project.

## Trust model

Before a client can talk to an EST server it needs a way to trust it — RFC 7030 calls this the explicit trust-anchor database bootstrap problem. Two solutions:

- **Implicit trust anchor.** The client already holds a CA certificate it trusts, baked in at manufacture or left over from a previous enrolment, and verifies the server's TLS certificate with it directly.
- **Explicit trust anchor via `cacerts`.** The client connects with minimal trust, fetches the chain over `GET /cacerts`, and verifies everything afterwards against the freshly retrieved chain.

Fetching the chain does not relax hostname verification. The server's own leaf certificate still has to satisfy a real hostname check, which is the single most common first-run failure.

## Operations

| Operation | Method | Purpose | Client auth |
|---|---|---|---|
| `cacerts` | GET | Fetch the CA chain — bootstrap trust, or refresh it | none |
| `csrattrs` | GET | Server states which CSR attributes it wants | none |
| `simpleenroll` | POST | Client submits its own CSR, receives a signed certificate | not required by the spec; deployments gate it — here, HTTP Basic against a directory |
| `simplereenroll` | POST | Renew a certificate the client already holds | mandatory: the client authenticates the TLS session with its current certificate |
| `serverkeygen` | POST | Server generates the keypair as well as signing | as `simpleenroll` |
| `fullcmc` | POST | Full CMC (RFC 5272) instead of the simplified PKCS#10/PKCS#7 flow | varies |

`simplereenroll` is the one operation the protocol says the server must not skip authentication on, which is why the iRule enforces it in the data path rather than leaving it to the backend.

## Transport encoding

Bodies use MIME types borrowed from S/MIME rather than raw binary or JSON. CSRs are base64 DER PKCS#10. Certificates and chains are base64 degenerate PKCS#7 — a `SignedData` with no signature, used as a container. `serverkeygen` responses are `multipart/mixed` carrying a certificate part and a key part. All of these should also carry `Content-Transfer-Encoding: base64`.

## Labels

An optional path segment — `/.well-known/est/<label>/simpleenroll` — lets one server front multiple CAs or policies. Here the label selects a BIG-IP pool.

## Adjacent specifications

**EST-coaps** (RFC 9148) adapts the same operations to CoAP for constrained devices. **BRSKI** (RFC 8995) builds zero-touch bootstrapping with ownership vouchers on top of EST. Neither is implemented here.
