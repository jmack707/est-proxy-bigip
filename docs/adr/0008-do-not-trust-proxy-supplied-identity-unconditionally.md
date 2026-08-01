# ADR-0008: Do not trust proxy-supplied identity unconditionally

## Status

Accepted

## Context

[ADR-0001](0001-terminate-tls-at-the-virtual-server.md) terminates TLS at the virtual server and forwards the client's identity to the backend as `X-SSL-Client-*` headers. The architecture page recorded the consequence — the hop is cleartext and must stay on a trusted segment — but the shim implemented that trust as *the header exists*, which turned out to be weaker than the design intended.

Two bypasses were demonstrated against a live deployment on 2026-08-01, against a real Active Directory and a BIG-IP virtual server:

**Unauthenticated issuance.** A `simplereenroll` sent straight to the backend port with `X-SSL-Client-Cert: totally-made-up` and no credentials at all returned `200` and a usable certificate for any name the PKI role allowed. `simplereenroll` is deliberately outside `LDAP_REQUIRE_OPS`, because RFC 7030 treats the existing certificate as the credential — but nothing verified that a certificate existed, so there was no credential at all.

**Name binding defeated by SAN.** An authenticated client sent a CSR whose CN matched its username, and SANs naming other principals. The role signs with `use_csr_sans`, so the SANs were copied into the issued certificate. TLS peers validate against SANs rather than the CN, so `LDAP_ENFORCE_CN_MATCH` — whose documented purpose is that one credential cannot mint a certificate naming somebody else — did not achieve it.

Both reduce to the same root cause: enforcement lived in the iRule, and the backend assumed the iRule was in the path.

## Decision

Make the backend verify what it previously assumed, in layers, so that no single control is load-bearing:

- **`EST_PROXY_SECRET`** — the iRule injects a shared secret; the shim rejects requests without it. This is what distinguishes "arrived through the BIG-IP" from "reached the port", and it is cheap, so it is the first line rather than the last.
- **`REENROLL_VERIFY_CERT`** (default on) — the forwarded certificate is verified against the issuing chain with `openssl verify`, covering signature, chain and expiry, instead of being taken on trust. The BIG-IP's own `X-SSL-Client-Verify` verdict is treated as advisory, because it is meaningful only when the `client-ssl` profile has a `ca-file`, which this project does not configure.
- **Name binding on both operations** — SANs are checked exactly as the CN is. On reenrolment the CSR must match the presented certificate: renewal replaces a certificate, it does not grant a new name.
- **`use_csr_sans=false`** on the signing role, so the CA also refuses to copy SANs it was not asked for.
- **`LDAP_CA_FILE`** — the directory's TLS certificate can now be verified, which it previously could not be at any setting.
- **`AUTH_MAX_FAILURES` / `AUTH_WINDOW_SECONDS`** — bound password guessing, and the account-lockout denial of service that unthrottled binds enable against a real directory.

## Consequences

**Makes easier:** the backend is defensible on its own. A misconfigured pool, a shared segment, or a host that can route to the listener no longer converts into arbitrary certificate issuance.

**Makes harder:** two values must now agree — `EST_PROXY_SECRET` on the shim and `--proxy-secret` on the deploy — and a mismatch presents as a uniform `403`. Enabling `LDAP_CA_FILE` also requires `LDAP_URI` to use a name the directory's certificate carries; pointing it at an IP fails hostname verification, which is correct but looks like a broken bundle.

**Costs:** the throttle is per-username and in-process. Behind SNAT every request shares one source address, so keying on the client address would not distinguish callers — which means an attacker who knows a username can deliberately throttle that user for the window. That is the deliberate trade: a local, self-clearing refusal in preference to lockout of the account in the real directory.

**Does not change:** the hop is still cleartext and still carries credentials. These controls raise the cost of reaching the listener; they do not make the segment safe, and [trust boundaries](../architecture.md#trust-boundaries) still governs.

Validated 2026-08-01 against BIG-IP VE 21.1, a Windows Server 2025 domain controller with an ADCS-issued LDAPS certificate, and OpenBao: both original exploits refused, legitimate enrolment and reenrolment unaffected, the throttle returning `429` after five failures, and directory TLS verification passing with the correct CA and failing with the wrong one. Both exploits are regression cases in `test-ldap-gate.sh`.

## Addendum — a full source review, 2026-08-02

A review of the whole tree surfaced four more, all now fixed:

- **Command injection as root on the BIG-IP (`bigip_lib.py`).** Object names — `--cert-name`, `--attach-profile` — were interpolated into tmsh strings run through `/mgmt/tm/util/bash`, i.e. `bash -c` on the device. Demonstrated with `--cert-name 'x$(id>/tmp/marker)'`, which ran `id` as `uid=0(root)`. The vector is command substitution, not `;` statement-breaking: `util/bash` tokenises `utilCmdArgs` rather than shell-splitting it, so a `;` becomes an inert argv token while `$(...)` inside the quoted command still evaluates. Reasoning about only `;` would have produced a fix that did not fix it. Names are now validated against `^[A-Za-z0-9._-]+$` before use. This matters most for automated enrolment, where the name derives from a device identity rather than an operator's keystroke.
- **iControl REST verified only when asked.** The management channel — carrying the operator password and, in `install_cert`, a private key — was unconditionally `CERT_NONE`. `BIGIP_CA_FILE` now enables full verification; unset keeps the self-signed-friendly default a fresh BIG-IP needs.
- **AppRole secret written world-readable.** `quickstart.sh` wrote `est-shim.env` under the default umask; it now forces `0600`.
- **`serverkeygen` bypassed the gate.** It issues a certificate but was absent from the default `LDAP_REQUIRE_OPS`, so enabling the directory gate still left it open. It is now in the default set.

The command-injection fix was verified on the live VE: the `$(id)` payload is refused before any REST call and writes nothing to the device, while a legitimate install still succeeds.
