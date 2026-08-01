# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Documentation set under `docs/`: architecture, EST protocol notes, five ADRs, install, deploy, upgrade, symptom-indexed troubleshooting, runbooks for certificate renewal and AppRole rotation, and configuration, CLI, and API references.
- `doc-standard.json` plus a `docs-lint` GitHub Action that fails a pull request on missing documents, missing sections, placeholder text, broken relative links, and drift between code and the reference pages.
- `teardown.sh`: reverses `quickstart.sh` from the same `deploy.env` — removes the BIG-IP virtual server, iRule, client-ssl profile, pool, and bootstrap certificate and key, and stops the backend containers. Refuses to run without `--yes`, printing what it would remove instead; `--bigip-only`, `--backend-only`, and `--purge` narrow or widen it. Works over iControl REST with the same management credentials as the deploy, so it needs no SSH to the BIG-IP. Object names follow `deploy_bigip.py`'s defaults and are overridable in `deploy.env` — the manual sequence previously documented in `docs/upgrade.md` hardcoded them despite all four being flags.
- `tests/irule_test.tcl`: an offline regression test for `est_proxy.irule.tcl` covering twelve routing cases, needing only `tclsh` and no BIG-IP. It defines `when` and sources the real iRule rather than a copy, stubbing the TMM commands it calls. Verified to fail — one case, exit `1` — against the pre-fix rule that returned `404` for unknown labels, so it genuinely guards that regression. Its limits are stated in the file and in `CONTRIBUTING.md`: stock Tcl is 8.6 where TMM reports 8.4, and TMM's `expr` word operators (`not`, `and`, `or`) are translated for stock Tcl, so a pass is evidence rather than proof and does not replace validation on hardware.

### Changed

- `README.md` reduced to orientation and links; protocol background moved to `docs/est-protocol.md`, procedures to `docs/install.md` and `docs/deploy.md`, and the "gotchas found the hard way" section to `docs/operations/troubleshooting.md` with a symptom index. No operational detail was dropped.
- Code comments that pointed at pre-restructure `README.md` sections ("Deploying" step 1, "Gotchas found the hard way", the "chicken-and-egg" note) now point at their `docs/` destinations.

### Removed

- `DEPLOY-AD.md`, dissolved into the docs tree so each instruction has one home: domain-controller LDAPS setup, the test user, the UPN-bind explanation, and the shim's LDAPS-certificate caveat moved to `docs/operations/ad-setup.md`; the topology diagram and the LDAPS caveat's trust-boundary consequence to `docs/architecture.md`; the `BAO_ADDR` container-vs-host note to the configuration reference and the `502` troubleshooting entry. Its OpenBao, shim, BIG-IP, and testing parts duplicated `docs/install.md` and `docs/deploy.md` and were dropped.

### Fixed

- `quickstart.sh` was committed without its executable bit (mode `100644`, where its sibling scripts are `100755`), so every clone and archive download produced a file that could not be run — while `README.md`, `docs/deploy.md`, `docs/reference/cli.md`, `deploy.env.example`, and the script's own header all instruct the reader to run `./quickstart.sh`. Reported from a fresh checkout as `bash: ./quickstart.sh: Permission denied`.

- `est_shim.py` dropped the connection instead of answering when the PKI backend was unreachable. The backend call sites caught only `urllib.error.HTTPError`, which covers "the backend answered with an error status"; a backend that is down, or a wrong `BAO_ADDR`, raises `URLError` — `HTTPError`'s parent — which escaped and killed the request handler, so the client got no response at all rather than the documented `502`. Observed on `cacerts`, which reaches OpenBao via `bao_raw_get` without a prior AppRole login; `simpleenroll` already answered `502` because the login attempt precedes signing and was guarded. All three sites (`ca_chain`, `sign`, `issue`) now return `502 OpenBao unreachable at <BAO_ADDR>: <reason>`. Reproduced and verified under Docker on Ubuntu 24.04.4, 2026-07-31.

- `deploy_bigip.py` prefixed `/Common/` onto object names unconditionally, so a fully qualified value such as `--clientssl-profile /Common/clientssl` became `/Common/Common/clientssl` and failed with `01020036:3: The requested profile ... was not found` — an error naming an object the caller never asked for. Names are now accepted bare or qualified, including partitions other than `/Common`. Affected `--vs-destination`, `--pool-name`, `--clientssl-profile`, and `--irule-name`; `--vs-vlan`, which previously required the qualified form, now takes either.

- `est_proxy.irule.tcl`: the default-pool fallback for unknown EST labels never matched — `static::est_label_pools("")` looks up a literal two-character `""` key in Tcl, not the empty-string key set in `RULE_INIT`, so any label not explicitly in the array got `404` instead of the default pool. Now uses the unquoted empty index.

  Validated on BIG-IP VE 21.1.0 build 0.0.38, 2026-07-31, against a standalone unit. `GET /.well-known/est/somelabel/cacerts` returns `200` from the default pool with the fix and `404 Unknown EST label: somelabel` with the pre-fix rule deployed alongside as a control. The unlabelled path was `200` in both, confirming the bug was confined to the fallback branch. Surrounding routing was re-checked on the same unit: non-EST path, unknown operation, and the `405`/`400`/`401` method, content-type, and missing-client-certificate cases all still refuse as documented.

  Worth recording for the next person: TMM's Tcl reports `info tclversion` as **8.4**, not the 8.6 an off-box `tclsh` check uses. The two interpreters agreed here — a probe iRule on the unit returned `quoted_exists=0`, `empty_exists=1` and a single key of length 0, matching stock 8.6 exactly — but the version gap is why an off-box check alone was not treated as sufficient.
