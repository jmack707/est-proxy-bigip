# ADR-0002: Bridge BIG-IP's own enrolment through an external `estclient`

## Status

Accepted

## Context

A BIG-IP fronting EST for other clients still needs certificates of its own, and the natural wish is for it to enrol itself. TMOS cannot: Certificate Order Manager integrates only with specific commercial CA vendor APIs, and BIG-IP 21.1's new certificate-automation feature was ACMEv2 — added because no built-in automated enrolment protocol existed before it. EST appears in neither that release's notes nor BIG-IP's certificate management documentation. This was checked against F5's documentation rather than assumed.

The options were to hand-roll an EST client in TCL or Python, or to drive the reference implementation externally and push the result onto the device.

## Decision

`bigip-est-enroll.py` performs the EST exchange externally using the real libest `estclient`, then installs the result through iControl REST and `tmsh` the way an operator would. It supports both `enroll` and RFC 7030 `renew`, reusing a previously issued certificate as TLS client identity for the latter.

## Consequences

**Makes easier:** the protocol exchange is done by the reference client, so wire-level conformance is not our problem — and several real bugs surfaced precisely because a strict client was used instead of a curl approximation. A renewal loop is available on a platform with no native EST client.

**Makes harder:** enrolment needs a host with `estclient` installed and reachability to both the EST endpoint and the management interface. It is not self-contained on the device, so it cannot be driven from TMOS alone.

**Commits us to:** an external scheduler for renewal — cron or a systemd timer on that host — and to keeping the issued pair somewhere for the next `renew`, since `simplereenroll` needs the current certificate as its credential.
