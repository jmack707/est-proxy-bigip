# ADR-0005: Accept unverified TLS to the PKI backend

## Status

Accepted — revisit before any production use

## Context

`est_shim.py` reaches the PKI backend over HTTPS using `ssl._create_unverified_context()`. In the lab the backend presents a certificate from the same lab CA the shim is helping to distribute, and in dev mode it may present a self-signed certificate on a loopback or private address. Verifying it properly means shipping a trust bundle to the shim and keeping it current — a real requirement, but one that adds a bootstrap step to a component whose value is being easy to stand up.

## Decision

Keep verification off, on the reasoning that the listener is internal-only, and record the compromise here rather than leaving it as an undocumented line of code.

## Consequences

**Makes easier:** the shim starts with no trust material of its own, so a lab stack comes up without a chicken-and-egg step.

**Makes harder:** an attacker positioned between the shim and the backend can impersonate the CA API. Given the shim holds a credential that can sign certificates, this is the weakest link in the design and should not be carried into production.

**Commits us to:** a follow-up before production: a `BAO_CACERT` setting feeding a verified `SSLContext`, with verification on by default. Until then, every deployment document says the shim-to-backend path must stay on a trusted segment, and this ADR is the one to supersede when that changes.
