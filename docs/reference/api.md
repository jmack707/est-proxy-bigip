# API reference

## Overview

Base path `/.well-known/est/`, with an optional label segment: `/.well-known/est/<label>/<operation>`. Two components enforce different things on the same request, and the split matters when you are reading a failure:

- **`est_proxy.irule.tcl`** on the BIG-IP: path and operation validity, HTTP method, request `Content-Type`, and the client-certificate requirement for `simplereenroll`. Rejections here never reach the backend.
- **`est_shim.py`** behind it: directory authentication, CSR handling, and everything involving the PKI backend.

The iRule lowercases the URI before matching, so operation names are case-insensitive at the virtual server. It also removes any inbound `X-SSL-Client-Cert`, `X-SSL-Client-Verify`, `X-SSL-Client-Subject`, and `X-SSL-Client-Serial` headers before inserting its own, so a client cannot assert its own TLS identity to the backend.

## Endpoints

| Path | Method | Client auth | Request type | Response type | Notes |
|---|---|---|---|---|---|
| `cacerts` | GET | none | — | `application/pkcs7-mime; smime-type=certs-only` | Proxies the backend's `<mount>/ca_chain`, wrapped as degenerate PKCS#7 with `Content-Transfer-Encoding: base64` |
| `csrattrs` | GET | none | — | `application/csrattrs` | Returns an empty body. Valid: the operation is optional in RFC 7030 |
| `simpleenroll` | POST | HTTP Basic → LDAP bind, when `LDAP_ENABLED` and the operation is in `LDAP_REQUIRE_OPS` | `application/pkcs10` | `application/pkcs7-mime; smime-type=certs-only` | Signs the client's CSR via `<mount>/sign/<role>`; CN-match enforced when `LDAP_ENFORCE_CN_MATCH` |
| `simplereenroll` | POST | TLS client certificate, **mandatory** | `application/pkcs10` | `application/pkcs7-mime; smime-type=certs-only` | Not LDAP-gated by design; the existing certificate is the authentication |
| `serverkeygen` | POST | same as `simpleenroll` | `application/pkcs10` | `multipart/mixed; boundary=estshimboundary` | Backend generates the keypair via `<mount>/issue/<role>` with CN `est-serverkeygen.<DOMAIN>`. Real EST clients currently fail to parse the returned key — see [troubleshooting](../operations/troubleshooting.md#serverkeygen-key-part-does-not-parse-in-the-client) |
| `fullcmc` | POST | — | `application/pkcs10` or `multipart/*` | — | The iRule accepts and validates it, but the backend does not implement it and answers `404`. CMC is a materially different encoding |

Labels: the iRule looks the label up in `static::est_label_pools` and selects that pool, falling back to the default entry. The backend parses the label too, but does not yet use it to select a different PKI mount or role.

## Status codes

| Code | Returned when | Returned by |
|---|---|---|
| `200` | operation succeeded | backend |
| `400` | request `Content-Type` is neither `application/pkcs10` nor `multipart/*` — checked for `simpleenroll`, `fullcmc`, and `serverkeygen`, but **not** for `simplereenroll`, where only the method and client certificate are enforced | iRule |
| `400` | `Authorization` header is malformed, or the CSR fails to parse | backend |
| `401` | `simplereenroll` with no client certificate in the TLS handshake | iRule, before the backend is reached |
| `401` | `simplereenroll` reaches the backend without `X-SSL-Client-Cert`, or Basic auth is required and absent (with `WWW-Authenticate: Basic realm="EST"`) | backend |
| `403` | LDAP bind failed, or the CSR CN does not match the authenticated user | backend |
| `404` | path outside `/.well-known/est`, unknown operation, or unknown label with no default pool | iRule |
| `404` | operation the backend does not implement, such as `fullcmc` | backend |
| `405` | wrong method for the operation, with an `Allow` header | iRule |
| `502` | backend could not reach the PKI: `ca_chain` fetch, AppRole login, `sign`, or `issue` failed | backend |

A `401` from the iRule and a `401` from the backend mean different things. The iRule's body says a client certificate is required; the backend's says the header is missing or Basic auth is needed. Read the body before concluding where the request stopped.

## Content types

Bodies use S/MIME-derived types rather than JSON:

- CSRs are base64-encoded DER PKCS#10 under `application/pkcs10`.
- Certificates and chains are base64-encoded **degenerate PKCS#7** — a `SignedData` with no signature, used purely as a certificate container — built by `openssl crl2pkcs7 -nocrl`, because the `cryptography` library cannot produce that structure.
- `serverkeygen` returns `multipart/mixed`: one `application/pkcs7-mime` part for the certificate, one `application/pkcs8` part for the key.
- Responses carry `Content-Transfer-Encoding: base64`, and the base64 is line-wrapped at 76 characters. Both the wrapping and the `HTTP/1.0` status line are compatibility requirements, not stylistic choices — see [ADR-0003](../adr/0003-http-1-0-and-wrapped-base64-for-libest.md).

The iRule logs a warning when a response `Content-Type` from the pool is not one of `application/pkcs7-mime*`, `application/csrattrs`, or `multipart/*`, which is a useful early signal that something other than the shim answered.
