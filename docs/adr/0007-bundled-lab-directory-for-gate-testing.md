# ADR-0007: Bundle a lab directory so the gate can be tested

## Status

Accepted

## Context

Directory-gated enrolment is the least-tested path in the project, and the reason is structural rather than negligent: exercising it requires a directory, and nothing in the repository provides one. [Contributing](../../CONTRIBUTING.md) requires four assertions before a pull request — missing credentials `401`, wrong password `403`, CSR CN not matching the authenticated user `403`, and correct credentials issuing a real certificate — and none of them can be run without first standing up Active Directory or FreeIPA by hand. A checklist the repository cannot execute is an honour system.

This is the same gap [ADR-0004](0004-dev-mode-openbao-for-lab-bootstrap.md) closed for the CA. Lab environments such as F5 UDF provide no directory any more than they provide a PKI, and the manual LDAPS setup is the last remaining hand step in every deployment path.

The obvious objection is that a bundled directory tests a directory nobody deploys. That objection is correct and is the reason for the constraints below rather than a reason not to do it.

## Decision

Ship a lab directory fixture — glauth, a single-binary LDAP server configured entirely by one committed file — behind a compose profile that is **off by default**, plus `test-ldap-gate.sh`, which turns the four required assertions into one command.

Three constraints make it honest:

**It reproduces FreeIPA's DN shape, not Active Directory's.** Bind DNs are `uid=<user>,cn=users,cn=accounts,<basedn>`. Active Directory additionally accepts a UPN (`user@domain`) as a bind DN, which is an AD-specific behaviour no generic LDAP server implements. A green run against this fixture says nothing about the AD path, which still requires a real domain controller.

**Its LDAPS certificate is issued by the lab CA**, not self-signed, so the fixture is reached over verified TLS using the same `ca-chain.pem` as everything else. A self-signed leaf is rejected by verifying LDAP clients, which would have pushed testing onto plaintext or onto disabled verification — either of which would quietly stop exercising the TLS path.

**It is named and gated as a fixture.** A separate compose file, a `lab-ldap` profile, and committed credentials that are documented in the open, in the same spirit as dev-mode OpenBao's known root token.

## Consequences

**Makes easier:** the gate becomes testable in CI and demonstrable without a customer directory. The negative cases stop being an honour system. A reviewer can run one command and see the refusals.

**Makes harder:** another container and another set of committed lab credentials to keep clearly labelled. There is a standing risk that a passing fixture run is mistaken for directory compatibility, which is why the limitation is stated here, in the configuration reference, and in the contributing guide rather than in one place.

**Does not prove:** Active Directory UPN binds, referrals, nested group evaluation, password policy or lockout behaviour, or anything about a directory under load. The fixture answers "does the gate refuse what it should", not "does this work against your directory".

**Also worth recording:** glauth does not implement the LDAP *Who am I?* extended operation, and its default ACL refuses search for the seeded users. Neither matters today because the shim performs a bind and nothing else — the CN check compares against the username from HTTP Basic, not against a directory attribute. If the shim ever needs to read attributes, this fixture needs revisiting rather than trusting.

Validated 2026-08 on Podman 5: certificate issued from a live OpenBao PKI role, glauth serving LDAPS with it, and a **verifying** client bind against `ca-chain.pem` succeeding, with wrong-password and unknown-user binds both returning `Invalid credentials (49)`.

The Active Directory path the fixture cannot cover was validated separately, against a real Windows Server 2025 domain controller for `f5lab.local` with an ADCS-issued LDAPS certificate: UPN binds succeeded, wrong password and unknown user both returned `data 52e`, and `test-ldap-gate.sh` passed all four assertions both directly against the shim and through a BIG-IP virtual server, finishing with a `simpleenroll` by the real `estclient` that verified clean. Two AD-specific constraints surfaced there and are recorded in [troubleshooting](../operations/troubleshooting.md#with-cn-enforcement-on-the-username-is-the-certificate-name): the 20-character `sAMAccountName` limit bounds how long an FQDN-shaped username can be, and AD's default password complexity rejects the fixture's own password.
