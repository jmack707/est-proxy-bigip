# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- **Unauthenticated certificate issuance via a forged identity header (critical).** `simplereenroll` checked only that `X-SSL-Client-Cert` was present, never that it held a real certificate, and it sits outside `LDAP_REQUIRE_OPS` by design. A request sent straight to `LISTEN_PORT` with a made-up header value and no credentials at all returned `200` and a usable certificate for any name the PKI role allowed. The forwarded certificate is now verified against the issuing chain (`REENROLL_VERIFY_CERT`, on by default), and `EST_PROXY_SECRET` lets the shim refuse anything that did not come through the iRule.
- **Name binding defeated by subject alternative names (high).** `LDAP_ENFORCE_CN_MATCH` compared only the CN, while the signing role copied CSR SANs into the issued certificate. Since TLS peers validate against SANs, an authenticated client could obtain a certificate naming other principals. SANs are now checked exactly as the CN is, on enrolment and reenrolment, and `bootstrap-openbao-dev.sh` sets `use_csr_sans=false` so the CA refuses them too.
- **Reenrolment could change the subject.** The CSR must now match the certificate presented: renewal replaces a certificate, it does not grant a new name.
- **The directory's TLS certificate can now be verified.** `LDAP_CA_FILE` switches `ldap3` to `CERT_REQUIRED`; previously no setting enabled verification, so `ldaps://` encrypted without authenticating the server.
- **Authentication is rate limited.** `AUTH_MAX_FAILURES` / `AUTH_WINDOW_SECONDS` bound password guessing and the account-lockout denial of service that unthrottled binds enable against a real directory.

- **Command injection as root on the BIG-IP (`bigip_lib.py`).** BIG-IP object names supplied as `--cert-name` / `--attach-profile` were interpolated into `tmsh` commands run through `/mgmt/tm/util/bash`, so a name like `x$(id)` executed as root on the device (confirmed `uid=0`). Names are now validated against `^[A-Za-z0-9._-]+$`. The vector is `$(...)`/backtick command substitution, not `;` — `util/bash` tokenises rather than shell-splits.
- **iControl REST TLS is now verifiable.** `BIGIP_CA_FILE` turns on full verification of the management channel in `deploy_bigip.py` and `bigip_lib.py`; previously it was unconditionally unverified, exposing the operator password and installed private keys to a MITM.
- **`quickstart.sh` writes `est-shim.env` as `0600`.** It previously used the default umask, leaving the `BAO_SECRET_ID` — a certificate-signing credential — world-readable on multi-user hosts.
- **`serverkeygen` is gated by default.** It issues a certificate but was missing from the default `LDAP_REQUIRE_OPS`, so enabling the directory gate still left it open to anyone who could reach the endpoint.

Both bypasses were demonstrated against a live deployment — real Active Directory, real BIG-IP virtual server — and are now regression cases in `test-ldap-gate.sh`. Rationale and trade-offs in [ADR-0008](docs/adr/0008-do-not-trust-proxy-supplied-identity-unconditionally.md).

### Added

- Documentation set under `docs/`: architecture, EST protocol notes, five ADRs, install, deploy, upgrade, symptom-indexed troubleshooting, runbooks for certificate renewal and AppRole rotation, and configuration, CLI, and API references.
- `doc-standard.json` plus a `docs-lint` GitHub Action that fails a pull request on missing documents, missing sections, placeholder text, broken relative links, and drift between code and the reference pages.
- `Dockerfile.estclient` and `estclient-docker.sh`: run the libest `estclient` from a container on hosts whose distribution has no `libest-utils` package. Arguments pass straight through; the image builds on first use; Docker and Podman both work. Validated on Podman 5 with `libest-utils` 3.2.0+ds-1.1 on Ubuntu 26.04. See [ADR-0006](docs/adr/0006-containerised-estclient-for-unpackaged-distros.md).
- Troubleshooting entries for `estclient` returning `0` after a failed exchange, and for `libest-utils` being absent before Ubuntu 25.10.
- `docs/operations/runbooks/first-install.md`: the ordered, mechanical path from an empty host to an issued certificate and a proven refusal path, with the layered verification sequence that localises a failure to one component.
- Lab directory fixture — `docker-compose.lab-ldap.yml`, `lab-ldap/glauth.cfg`, and `lab-ldap/gen-cert.sh` — so directory-gated enrolment can be exercised where no directory exists. Off by default behind a `lab-ldap` compose profile; its LDAPS certificate is issued by the lab CA so the fixture is reached over verified TLS. It reproduces FreeIPA's DN shape and **not** Active Directory's UPN bind; see [ADR-0007](docs/adr/0007-bundled-lab-directory-for-gate-testing.md). Validated on Podman 5 with a live OpenBao PKI role.
- `test-ldap-gate.sh`: turns the four gate assertions the contributing guide requires into one command that exits non-zero on the first mismatch, and checks the success case decodes as PKCS#7 rather than trusting a `200`.
- Troubleshooting note that a PKI role refuses short-hostname SANs the same way it refuses out-of-domain CNs, with the message it actually produces.
- Troubleshooting entry for the interaction between `LDAP_ENFORCE_CN_MATCH` and the PKI role's `allowed_domains`: the comparison is exact string equality, so a gated deployment's usernames must be the certificate names. Includes the Active Directory guidance found while validating it: bind by `userPrincipalName` and let `sAMAccountName` be derived, which sidesteps its 20-character cap entirely — a 40-character UPN prefix was confirmed to enrol end to end.
- Troubleshooting entry recording that the shim does not verify the directory's TLS certificate: `ldap3` is given no CA bundle and defaults to `CERT_NONE`, so `ldaps://` encrypts without authenticating the server, and no setting changes it.

### Changed

- `test-ldap-gate.sh` and the lab fixture now use FQDN-shaped usernames (`client1.example.com`). The first version assumed the shim compared the CN's first label to the username; it compares the whole string, so the original users could never have enrolled.

### Fixed

- Documentation said `apt install libest-utils` with no minimum release, in `docs/install.md`, `CONTRIBUTING.md`, `docs/reference/cli.md`, and the certificate renewal runbook. The package exists only in Ubuntu 25.10 and newer, so the instruction failed outright on older hosts.
- The renewal runbook's health check treated `estclient`'s exit status as a success signal. It returns `0` for a failed exchange, so the check now asserts on the output file.
- `teardown.sh`: reverses `quickstart.sh` from the same `deploy.env` — removes the BIG-IP virtual server, iRule, client-ssl profile, pool, and bootstrap certificate and key, and stops the backend containers. Refuses to run without `--yes`, printing what it would remove instead; `--bigip-only`, `--backend-only`, and `--purge` narrow or widen it. Works over iControl REST with the same management credentials as the deploy, so it needs no SSH to the BIG-IP. Object names follow `deploy_bigip.py`'s defaults and are overridable in `deploy.env` — the manual sequence previously documented in `docs/upgrade.md` hardcoded them despite all four being flags.
- `tests/irule_test.tcl`: an offline regression test for `est_proxy.irule.tcl` covering twelve routing cases, needing only `tclsh` and no BIG-IP. It defines `when` and sources the real iRule rather than a copy, stubbing the TMM commands it calls. Verified to fail — one case, exit `1` — against the pre-fix rule that returned `404` for unknown labels, so it genuinely guards that regression. Its limits are stated in the file and in `CONTRIBUTING.md`: stock Tcl is 8.6 where TMM reports 8.4, and TMM's `expr` word operators (`not`, `and`, `or`) are translated for stock Tcl, so a pass is evidence rather than proof and does not replace validation on hardware.

### Changed

- `README.md` reduced to orientation and links; protocol background moved to `docs/est-protocol.md`, procedures to `docs/install.md` and `docs/deploy.md`, and the "gotchas found the hard way" section to `docs/operations/troubleshooting.md` with a symptom index. No operational detail was dropped.
- Code comments that pointed at pre-restructure `README.md` sections ("Deploying" step 1, "Gotchas found the hard way", the "chicken-and-egg" note) now point at their `docs/` destinations.

### Removed

- `DEPLOY-AD.md`, dissolved into the docs tree so each instruction has one home: domain-controller LDAPS setup, the test user, the UPN-bind explanation, and the shim's LDAPS-certificate caveat moved to `docs/operations/ad-setup.md`; the topology diagram and the LDAPS caveat's trust-boundary consequence to `docs/architecture.md`; the `BAO_ADDR` container-vs-host note to the configuration reference and the `502` troubleshooting entry. Its OpenBao, shim, BIG-IP, and testing parts duplicated `docs/install.md` and `docs/deploy.md` and were dropped.

### Fixed

- `deploy_bigip.py` skipped every object that already existed, so a re-run could not repair a broken deployment — and reported success while leaving it broken. Two consequences were real: a pool that already existed kept whatever members it had, so a partial earlier run left `est-backend-pool` with **no member** and every later run said `already exists (ok)`; and an edited `est_proxy.irule.tcl` was never uploaded to an existing rule, which is exactly what step 4 of the upgrade procedure depends on. The pool's members and the iRule's body — the two objects this script owns the contents of — are now reconciled with a `PATCH` on that branch, reported as `already exists, reconciled`. A reconcile that fails now exits non-zero instead of printing success. The client-ssl profile and virtual server are still created-then-left, deliberately: `install-cert-bigip.py` owns the profile's `cert-key-chain`, and repointing a live virtual server's listener should not be a side effect of a re-run.

- `quickstart.sh` was committed without its executable bit (mode `100644`, where its sibling scripts are `100755`), so every clone and archive download produced a file that could not be run — while `README.md`, `docs/deploy.md`, `docs/reference/cli.md`, `deploy.env.example`, and the script's own header all instruct the reader to run `./quickstart.sh`. Reported from a fresh checkout as `bash: ./quickstart.sh: Permission denied`.

- `est_shim.py` dropped the connection instead of answering when the PKI backend was unreachable. The backend call sites caught only `urllib.error.HTTPError`, which covers "the backend answered with an error status"; a backend that is down, or a wrong `BAO_ADDR`, raises `URLError` — `HTTPError`'s parent — which escaped and killed the request handler, so the client got no response at all rather than the documented `502`. Observed on `cacerts`, which reaches OpenBao via `bao_raw_get` without a prior AppRole login; `simpleenroll` already answered `502` because the login attempt precedes signing and was guarded. All three sites (`ca_chain`, `sign`, `issue`) now return `502 OpenBao unreachable at <BAO_ADDR>: <reason>`. Reproduced and verified under Docker on Ubuntu 24.04.4, 2026-07-31.

- `deploy_bigip.py` prefixed `/Common/` onto object names unconditionally, so a fully qualified value such as `--clientssl-profile /Common/clientssl` became `/Common/Common/clientssl` and failed with `01020036:3: The requested profile ... was not found` — an error naming an object the caller never asked for. Names are now accepted bare or qualified, including partitions other than `/Common`. Affected `--vs-destination`, `--pool-name`, `--clientssl-profile`, and `--irule-name`; `--vs-vlan`, which previously required the qualified form, now takes either.

- `est_proxy.irule.tcl`: the default-pool fallback for unknown EST labels never matched — `static::est_label_pools("")` looks up a literal two-character `""` key in Tcl, not the empty-string key set in `RULE_INIT`, so any label not explicitly in the array got `404` instead of the default pool. Now uses the unquoted empty index.

  Validated on BIG-IP VE 21.1.0 build 0.0.38, 2026-07-31, against a standalone unit. `GET /.well-known/est/somelabel/cacerts` returns `200` from the default pool with the fix and `404 Unknown EST label: somelabel` with the pre-fix rule deployed alongside as a control. The unlabelled path was `200` in both, confirming the bug was confined to the fallback branch. Surrounding routing was re-checked on the same unit: non-EST path, unknown operation, and the `405`/`400`/`401` method, content-type, and missing-client-certificate cases all still refuse as documented.

  Worth recording for the next person: TMM's Tcl reports `info tclversion` as **8.4**, not the 8.6 an off-box `tclsh` check uses. The two interpreters agreed here — a probe iRule on the unit returned `quoted_exists=0`, `empty_exists=1` and a single key of length 0, matching stock 8.6 exactly — but the version gap is why an off-box check alone was not treated as sufficient.
