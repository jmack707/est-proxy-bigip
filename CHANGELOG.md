# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Documentation set under `docs/`: architecture, EST protocol notes, five ADRs, install, deploy, upgrade, symptom-indexed troubleshooting, runbooks for certificate renewal and AppRole rotation, and configuration, CLI, and API references.
- `doc-standard.json` plus a `docs-lint` GitHub Action that fails a pull request on missing documents, missing sections, placeholder text, broken relative links, and drift between code and the reference pages.

### Changed

- `README.md` reduced to orientation and links; protocol background moved to `docs/est-protocol.md`, procedures to `docs/install.md` and `docs/deploy.md`, and the "gotchas found the hard way" section to `docs/operations/troubleshooting.md` with a symptom index. No operational detail was dropped.
- `DEPLOY-AD.md` added to the docs lint's scan globs, so the long-form walkthrough is now link- and placeholder-checked like the rest of the documentation.
- Code comments that pointed at pre-restructure `README.md` sections ("Deploying" step 1, "Gotchas found the hard way", the "chicken-and-egg" note) now point at their `docs/` destinations.

### Fixed

- `est_proxy.irule.tcl`: the default-pool fallback for unknown EST labels never matched — `static::est_label_pools("")` looks up a literal two-character `""` key in Tcl, not the empty-string key set in `RULE_INIT`, so any label not explicitly in the array got `404` instead of the default pool. Now uses the unquoted empty index. Semantics verified with tclsh 8.6; not yet re-validated on a live BIG-IP, so labeled routing keeps its "verified" claim only for labels explicitly present in the array.
