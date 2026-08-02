# est-proxy-bigip

Turn an F5 BIG-IP virtual server into an **RFC 7030 (EST) proxy** in front of a HashiCorp Vault or OpenBao PKI backend, with optional FreeIPA or Active Directory client authentication — a working iRule, a minimal containerisable EST server, and a deploy script, verified end to end with the real Cisco `libest` client (`estclient`), not a `curl` approximation.

## What this is

EST is the IETF standard for automated certificate enrolment used by network devices, IoT, and PKI clients that need something more structured than ACME. BIG-IP does not speak it natively; this project makes a virtual server behave like an EST server by terminating and inspecting TLS at the iRule layer and translating RFC 7030 operations to a CA backend.

It is not a general-purpose EST implementation. `fullcmc` is not implemented, `csrattrs` returns empty, and `serverkeygen` returns data that real clients cannot yet parse. See [constraints and non-goals](docs/architecture.md#constraints-and-non-goals).

## Quickstart

No existing Vault or OpenBao, no BIG-IP objects deployed? One file, one script:

```bash
cp deploy.env.example deploy.env   # fill in your BIG-IP/backend/AD details
./quickstart.sh
```

This runs the stack up to a testable state non-interactively: a throwaway dev-mode OpenBao, its PKI bootstrapped, the EST shim configured and started, the BIG-IP pool, profile, iRule, and virtual server deployed, and the virtual server's own bootstrap certificate issued and installed. Safe to re-run — the OpenBao bootstrap and the BIG-IP deploy are both idempotent.

Not production-ready as configured: dev-mode OpenBao is in-memory with a known root token ([ADR-0004](docs/adr/0004-dev-mode-openbao-for-lab-bootstrap.md)), and TLS to the PKI backend is unverified ([ADR-0005](docs/adr/0005-unverified-tls-to-the-pki-backend.md)). Still manual: AD or FreeIPA LDAPS setup, and testing with `estclient` — see [deploy](docs/deploy.md).

## Topology

```text
EST client --> BIG-IP virtual server (TLS terminates here)
               client-ssl profile: peerCertMode=request,
               cert issued by your CA, CN/SAN = the VS hostname
            -> iRule est_proxy (method/content-type checks,
               client-cert enforcement for reenroll,
               X-SSL-Client-* header injection)
            -> pool -> est_shim.py (plain HTTP)
            -> Vault/OpenBao PKI secrets engine (issue/sign/ca_chain)
```

The hop from the virtual server to the shim is cleartext and carries the client's identity as headers, so it has to stay on a trusted segment — see [trust boundaries](docs/architecture.md#trust-boundaries).

## Components

| File | Responsibility |
|---|---|
| `est_proxy.irule.tcl` | Enforces RFC 7030 method and content-type rules per operation, requires a client certificate for `simplereenroll` before the request reaches the backend, forwards TLS identity as `X-SSL-Client-*` headers, routes by EST label to multiple pools |
| `est_shim.py` | Minimal EST server; implements `cacerts`, `simpleenroll`, `simplereenroll`, `serverkeygen` against a Vault/OpenBao PKI, with optional FreeIPA/AD authentication. One optional dependency (`ldap3`) |
| `deploy_bigip.py` | Creates the pool, `client-ssl` profile, iRule, and virtual server over iControl REST. Idempotent |
| `bigip-est-enroll.py` | Enrols or renews a certificate *for* a BIG-IP by driving the real `estclient`, since TMOS has no EST client ([ADR-0002](docs/adr/0002-external-estclient-bridge-for-bigip-certificates.md)) |
| `install-cert-bigip.py` | Installs a PEM cert/key pair you already hold, used for the virtual server's own bootstrap certificate |
| `bigip_lib.py` | Shared iControl REST helpers, so BIG-IP-side logic exists in one place |
| `bootstrap-openbao-dev.sh` | Stands up a throwaway dev-mode OpenBao with a root CA, intermediate, signing role, and scoped AppRole |
| `quickstart.sh` | Runs all of the above from a single config file |
| `est-shim.service`, `est-shim.env.example` | systemd unit and configuration template for the host-process path |
| `Dockerfile`, `docker-compose.yml`, `requirements.txt` | Container path, and a compose stack including the lab PKI |
| `Dockerfile.estclient`, `estclient-docker.sh` | Runs the libest `estclient` from a container, for hosts with no `libest-utils` package ([ADR-0006](docs/adr/0006-containerised-estclient-for-unpackaged-distros.md)) |
| `docker-compose.lab-ldap.yml`, `lab-ldap/` | Throwaway LDAP fixture so the directory gate can be exercised without a real directory — lab-only, off by default ([ADR-0007](docs/adr/0007-bundled-lab-directory-for-gate-testing.md)) |
| `test-ldap-gate.sh` | Asserts the gate refuses: no credentials `401`, wrong password `403`, CN mismatch `403`, correct credentials a real certificate |
| `deploy.env.example` | Single configuration file for `quickstart.sh` |

<!-- doclint:ignore DOC014 -- reports results; the commands that reproduce them live in docs/deploy.md -->
## Verification

Validated on real infrastructure, not unit-tested in isolation: BIG-IP VE 21.1, OpenBao 2.2.0, and a real FreeIPA server.

- `cacerts` — decoded chain matches the configured root and intermediate.
- `simpleenroll` — issued certificate verifies clean with `openssl verify`.
- `simplereenroll` — accepts the previously issued certificate as TLS client identity and returns a fresh one, also verifying clean. A `simplereenroll` with no client certificate is refused with `401` by the iRule, before the backend is reached.
- `serverkeygen` — backend logic verified with `curl`; the real client's multipart parsing is a known gap.
- `bootstrap-openbao-dev.sh` — every command run against a fresh OpenBao 2.2.0: root CA, intermediate signed and set, signing role, AppRole, and the resulting credentials serving `cacerts` and `simpleenroll` through a live shim.
- LDAP authentication — against a real FreeIPA LDAPS bind, not a mock: no credentials `401`, wrong password `403`, CSR CN not matching the authenticated user `403`, correct credentials with matching CN a real certificate. Verified running the shim as a container as well as a host process.
- `bigip-est-enroll.py` — `enroll`, `renew`, and `--attach-profile` against a real BIG-IP: installed objects match the requested CN, issuer, and SAN; `renew` produces a genuinely new certificate (different fingerprint and expiry, confirmed before and after); the profile's `cert-key-chain` updates to the new objects.

Three real bugs were found this way that reading the code would not have caught, plus two BIG-IP `tmsh`/bash quirks and a bash parameter-expansion gotcha — all in [troubleshooting](docs/operations/troubleshooting.md).

## Documentation

| Page | For |
|---|---|
| [Architecture](docs/architecture.md) | how the pieces fit, data flow, trust boundaries, non-goals |
| [EST protocol notes](docs/est-protocol.md) | RFC 7030 background: trust model, operations, encodings, labels |
| [Decisions](docs/adr/) | why it is built this way, and what each choice costs |
| [Install](docs/install.md) | prerequisites with tested versions, backend setup, verification |
| [Deploy](docs/deploy.md) | BIG-IP objects, the fast path, idempotency, rollback |
| [Upgrade](docs/upgrade.md) | version moves, rollback, teardown |
| [AD/LDAPS setup](docs/operations/ad-setup.md) | enabling LDAPS on a domain controller, test users, the shim's LDAPS caveat |
| [Troubleshooting](docs/operations/troubleshooting.md) | symptom-first index of every failure mode found so far |
| [Runbooks](docs/operations/runbooks/) | first install, certificate renewal, AppRole rotation |
| [Configuration reference](docs/reference/configuration.md) | every environment variable, its default and effect |
| [CLI reference](docs/reference/cli.md) | every script, flag, and exit code |
| [API reference](docs/reference/api.md) | endpoints, status codes, content types, and which component enforces what |
| [Contributing](CONTRIBUTING.md) | development setup, testing expectations, docs standard |

## License

MIT — see [LICENSE](LICENSE).
