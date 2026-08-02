# ADR-0006: Ship a containerised `estclient` for distributions that do not package it

## Status

Accepted

## Context

Every testing instruction in this repository, and `bigip-est-enroll.py` itself, depends on the libest `estclient` binary, and pointed at `apt install libest-utils`. That package first appears in **Ubuntu 25.10 (questing)**, and is also in 26.04 LTS (resolute) and 26.10 — version `3.2.0+ds-1.1` in all three, in `universe`. It exists in no earlier release, in any component.

Jump hosts in the environments this project targets are routinely older. The instruction was followed on an Ubuntu 22.04 UDF host and failed with `E: Unable to locate package libest-utils`, including after `add-apt-repository universe`, which reads as a broken repository configuration rather than a wrong release — the docs otherwise pin tested versions carefully, so an unqualified `apt install` line looks verified.

The alternative is building libest from source. That needs a compiler toolchain and OpenSSL headers, `./configure --with-ssl-dir`, and `LD_LIBRARY_PATH` set at every invocation to find `libest.so`. It produces an untracked binary whose version nobody records, which sits badly with a repository that holds itself to stating what it validated against.

Refusing to test on older hosts is not available either: `curl` is not a substitute. Three of the failure modes in [troubleshooting](../operations/troubleshooting.md) pass a `curl` test and fail a real client ([ADR-0003](0003-http-1-0-and-wrapped-base64-for-libest.md)).

## Decision

Ship `Dockerfile.estclient` and an `estclient-docker.sh` wrapper that runs the packaged `estclient` from a container built on a release that has it, defaulting to `ubuntu:26.04`. Arguments pass straight through, so it substitutes for the binary in every documented command. The docs keep the native `apt install libest-utils` as the first option and name the minimum release, rather than making everyone use a container.

## Consequences

**Makes easier:** testing with the reference client on any host with a container runtime, at a pinned and stated package version, with nothing installed on the host. The wrapper also builds on first use, so there is no separate setup step to forget.

**Makes harder:** a container runtime becomes a testing prerequisite where previously a package would have done, and the container has its own `/etc/hosts` — a virtual server hostname resolved by a host `/etc/hosts` entry is invisible inside it, which is why the wrapper exposes `ESTCLIENT_ADD_HOST`. Client keys and certificates must live in the mounted output directory to be reachable by `-x`, `-c`, and `-k`.

**Commits us to:** tracking which releases carry `libest-utils`. When the oldest supported jump host catches up, this becomes removable rather than permanent.

Validated 2026-07 on Podman 5 against `ubuntu:26.04`, yielding `libest-utils` 3.2.0+ds-1.1: image build, argument pass-through, output and trust-chain mounts, `--add-host`, first-use build followed by a cached second run, and both guard paths (missing runtime, missing CA chain) exiting `2`. Exit-status propagation through the wrapper was verified separately and is faithful — `estclient` itself is what returns `0` on a failed exchange ([troubleshooting](../operations/troubleshooting.md#estclient-exits-0-on-a-failed-operation)).
