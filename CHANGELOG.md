# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Documentation set under `docs/`: architecture, EST protocol notes, five ADRs, install, deploy, upgrade, symptom-indexed troubleshooting, runbooks for certificate renewal and AppRole rotation, and configuration, CLI, and API references.
- `doc-standard.json` plus a `docs-lint` GitHub Action that fails a pull request on missing documents, missing sections, placeholder text, broken relative links, and drift between code and the reference pages.

### Changed

- `README.md` reduced to orientation and links; protocol background moved to `docs/est-protocol.md`, procedures to `docs/install.md` and `docs/deploy.md`, and the "gotchas found the hard way" section to `docs/operations/troubleshooting.md` with a symptom index. No operational detail was dropped.
- Code comments that pointed at pre-restructure `README.md` sections ("Deploying" step 1, "Gotchas found the hard way", the "chicken-and-egg" note) now point at their `docs/` destinations.

### Removed

- `DEPLOY-AD.md`, dissolved into the docs tree so each instruction has one home: domain-controller LDAPS setup, the test user, the UPN-bind explanation, and the shim's LDAPS-certificate caveat moved to `docs/operations/ad-setup.md`; the topology diagram and the LDAPS caveat's trust-boundary consequence to `docs/architecture.md`; the `BAO_ADDR` container-vs-host note to the configuration reference and the `502` troubleshooting entry. Its OpenBao, shim, BIG-IP, and testing parts duplicated `docs/install.md` and `docs/deploy.md` and were dropped.

### Fixed

- `est_proxy.irule.tcl`: the default-pool fallback for unknown EST labels never matched — `static::est_label_pools("")` looks up a literal two-character `""` key in Tcl, not the empty-string key set in `RULE_INIT`, so any label not explicitly in the array got `404` instead of the default pool. Now uses the unquoted empty index. Semantics verified with tclsh 8.6; not yet re-validated on a live BIG-IP, so labeled routing keeps its "verified" claim only for labels explicitly present in the array.
