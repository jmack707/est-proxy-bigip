# ADR-0003: Serve `HTTP/1.0` with line-wrapped base64 for libest compatibility

## Status

Accepted

## Context

The reference EST client is Cisco's libest, and real clients in the field are built on it. Two of its behaviours constrain any server it talks to, both found by reading `src/est/est_client.c` rather than by guessing:

Its HTTP status-line parser accepts only `HTTP/1.0`; a well-formed `HTTP/1.1` response fails immediately as an unhandled HTTP response. Its base64 decoder is a raw `BIO_f_base64()`, which expects PEM-style periodic newlines; Python's `base64.b64encode()` emits one unbroken line, which silently breaks the decode with no OpenSSL error queued at all.

## Decision

`est_shim.py` sets `protocol_version = "HTTP/1.0"` and encodes response bodies with `base64.encodebytes()`, which wraps at 76 characters in MIME style.

## Consequences

**Makes easier:** real clients work. Both symptoms are silent or misleading when you get them wrong, so encoding for the strict parser costs nothing and removes a whole class of unexplainable failures.

**Makes harder:** no keep-alive, and every response closes the connection. Fine at enrolment volumes; not something to reuse for a high-throughput service.

**Commits us to:** treating libest's parser as the compatibility target. Anyone tempted to switch to `b64encode` for tidiness, or to enable HTTP/1.1 for performance, will break clients in a way that does not look like an encoding bug — hence this record.
