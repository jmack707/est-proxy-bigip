# Contributing

## Development setup

The shim is a single stdlib-only Python file plus one optional dependency, so no build step and no virtualenv are needed to run it. What you do need is somewhere to exercise it against:

```bash
pip install -r requirements.txt        # only for LDAP_ENABLED=true
docker compose up -d openbao
BAO_ADDR=http://127.0.0.1:8200 ./bootstrap-openbao-dev.sh test.local
```

For anything touching the iRule or the BIG-IP scripts you need a real BIG-IP — a UDF blueprint or a local VE. The `openssl` CLI is a hard dependency; the shim shells out to it for PKCS#7 packaging and CSR parsing.

## Testing

Test with the real `estclient` (`apt install libest-utils`), not `curl`. This is not a preference. Three of the bugs recorded in [troubleshooting](docs/operations/troubleshooting.md) pass a `curl` test and fail a real client, because `curl` tolerates a missing TLS `close_notify`, accepts `HTTP/1.1`, and does not use libest's strict base64 decoder.

Before opening a pull request:

- `cacerts`, `simpleenroll`, and `simplereenroll` succeed against a live BIG-IP, with the issued certificate verifying clean via `openssl verify`.
- The negative cases still refuse: `simplereenroll` with no client certificate gives `401` from the iRule; with `LDAP_ENABLED=true`, missing credentials give `401`, a wrong password `403`, and a CSR CN not matching the authenticated user `403`.
- Anything claimed as validated states the platform versions and the date it was checked.

Findings discovered empirically are worth more than the fix alone. Record the mechanism and how it was confirmed, so the next person can tell a verified finding from a guess.

## Documentation

This repository follows the documentation standard in `doc-standard.json`. A change that adds or alters an environment variable, a script, a flag, or an endpoint updates the matching reference page in the same pull request — the lint checks for exactly that drift and will fail the PR.

A design choice that a reasonable engineer would have made differently, or that a vendor limitation forced, gets an ADR under `docs/adr/` rather than a code comment.

```bash
python3 .github/scripts/doc_lint.py            # what CI will report
python3 .github/scripts/doc_lint.py --strict   # warnings fail too
```

## Pull requests

One concern per pull request. Say in the description what you validated it against, including versions — "validated against BIG-IP VE 21.1 and OpenBao 2.2.0" is the standard the rest of the repository is held to. If something is a known gap rather than a working feature, say so in the documentation as well as the PR.
