# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Documentation set under `docs/`: architecture, EST protocol notes, five ADRs, install, deploy, upgrade, symptom-indexed troubleshooting, runbooks for certificate renewal and AppRole rotation, and configuration, CLI, and API references.
- `doc-standard.json` plus a `docs-lint` GitHub Action that fails a pull request on missing documents, missing sections, placeholder text, broken relative links, and drift between code and the reference pages.

### Changed

- `README.md` reduced to orientation and links; protocol background moved to `docs/est-protocol.md`, procedures to `docs/install.md` and `docs/deploy.md`, and the "gotchas found the hard way" section to `docs/operations/troubleshooting.md` with a symptom index. No operational detail was dropped.
