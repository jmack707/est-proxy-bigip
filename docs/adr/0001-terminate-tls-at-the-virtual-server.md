# ADR-0001: Terminate TLS at the virtual server and forward client identity as headers

## Status

Accepted

## Context

EST's authentication model is bound to the TLS session. `simplereenroll` authenticates with the client's existing certificate, and RFC 7030 treats that as the operation's authentication rather than something layered above it. The CA backend is a Vault-API-compatible PKI whose HTTP API has no concept of a TLS client certificate presented by a third party.

Something therefore has to observe the handshake and communicate what it saw to the component that talks to the CA. BIG-IP already terminates TLS and can inspect the handshake from an iRule; re-establishing TLS to the backend would mean either passing the client certificate through some other channel anyway, or moving enforcement into the backend and losing the ability to reject before the request reaches it.

## Decision

The virtual server terminates TLS with a `client-ssl` profile set to `peerCertMode: request`. The iRule enforces the protocol rules that depend on the handshake — mandatory client certificate for `simplereenroll` — and forwards the client's identity to the pool as `X-SSL-Client-Cert`, `X-SSL-Client-Verify`, `X-SSL-Client-Subject`, and `X-SSL-Client-Serial`. It removes any inbound copies of those headers first. The backend listens plain HTTP and trusts them.

`simplereenroll` is not additionally gated on the directory, because the certificate already establishes identity and layering LDAP on top would verify the same identity twice by two mechanisms.

## Consequences

**Makes easier:** invalid requests are rejected in the data path, before the backend or the CA sees them. The backend stays small — no TLS, no certificate parsing for authentication purposes — and can run as a container with one optional dependency.

**Makes harder:** the hop from virtual server to backend is cleartext and must stay on a trusted segment. The header-based identity is only trustworthy because the iRule strips inbound copies; that stripping is load-bearing security, not hygiene.

**Commits us to:** treating `LISTEN_PORT` as a privileged listener. Any path to it that bypasses the virtual server bypasses the client-certificate requirement for `simplereenroll` entirely, so network controls around the backend are part of the security model, not an optional hardening step.
