# ADR-0004: Use dev-mode OpenBao for lab bootstrap

## Status

Accepted

## Context

The backend needs any Vault-API-compatible PKI. Lab environments such as F5 UDF provide none, and a real cluster with an init and unseal ceremony is a poor fit for a blueprint that gets torn down and rebuilt repeatedly. Every command in the bootstrap was validated against a real OpenBao 2.2.0 instance rather than copied from documentation.

## Decision

`bootstrap-openbao-dev.sh` and `docker-compose.yml` stand up dev-mode OpenBao — in-memory, auto-unsealed, known root token — and wire up a root CA, an intermediate, a signing role, and a scoped AppRole in one run. Every document that mentions it states that this is lab-only and why.

## Consequences

**Makes easier:** a complete working stack in one command, with no unseal ceremony and nothing to clean up. `est_shim.py` is unaffected either way, since it only speaks the Vault HTTP API.

**Makes harder:** nothing survives a restart, and the root token is known ahead of time. A demo that quietly becomes a dependency would be a real problem, which is why the warning is repeated rather than mentioned once.

**Commits us to:** documenting the production path separately. Pointing the shim at a real cluster is a configuration change plus an AppRole scoped to `ca_chain`, `sign/<role>`, and `issue/<role>` — no code change — and that boundary is the whole reason the choice is safe to offer.
