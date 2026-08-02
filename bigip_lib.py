#!/usr/bin/env python3
"""Shared iControl REST helpers for pushing a cert/key onto a BIG-IP.
Used by both install-cert-bigip.py (direct install, e.g. a VS's own
bootstrap cert) and bigip-est-enroll.py (install after an EST enrollment).
Stdlib only.
"""
import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# tmsh commands are assembled by interpolation and run through
# /mgmt/tm/util/bash, i.e. `bash -c "<command>"` on the device. A value
# carrying shell metacharacters -- notably `$(...)` or backticks, which the
# earlier `;`-based reasoning misses because util/bash tokenises rather than
# shell-splits -- executes as root on the BIG-IP. Names that reach a tmsh
# string must therefore be constrained to what a BIG-IP object name can
# actually be. This matters most for automated enrolment, where the name may
# derive from a device identity rather than an operator's keystroke.
_BIGIP_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def valid_bigip_name(name):
    return bool(name) and len(name) <= 128 and _BIGIP_NAME_RE.match(name) is not None


def require_bigip_name(name, what):
    if not valid_bigip_name(name):
        die(f"illegal {what}: {name!r} -- only letters, digits, dot, underscore "
            f"and hyphen are allowed (max 128 chars)")


def bigip_ssl_context():
    """TLS context for iControl REST.

    Verification is off by default because a fresh BIG-IP presents a
    self-signed management certificate, and the operator credentials plus, in
    install_cert, a private key cross this channel. Set BIGIP_CA_FILE to a
    bundle that signs the management certificate to turn on full verification;
    that is the production posture and this is the lab default.
    """
    ca_file = os.environ.get("BIGIP_CA_FILE", "")
    if ca_file:
        ctx = ssl.create_default_context(cafile=ca_file)
        return ctx  # check_hostname and CERT_REQUIRED are the defaults here
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def bigip_client(host, user, password):
    ctx = bigip_ssl_context()
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()

    def req(method, path, data=None, extra_headers=None, ctype="application/json"):
        headers = {"Authorization": f"Basic {auth}"}
        if extra_headers:
            headers.update(extra_headers)
        if data is not None and ctype:
            headers["Content-Type"] = ctype
        r = urllib.request.Request(f"https://{host}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, context=ctx, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    return req


def upload_to_bigip(req, filename, content_bytes):
    size = len(content_bytes)
    chunk = 512 * 1024
    start = 0
    while start < size:
        end = min(start + chunk, size)
        st, body = req("POST", f"/mgmt/shared/file-transfer/uploads/{filename}",
                        data=content_bytes[start:end],
                        extra_headers={"Content-Range": f"{start}-{end - 1}/{size}"},
                        ctype="application/octet-stream")
        if st not in (200, 202):
            die(f"upload of {filename} failed @{start}: HTTP {st} {body[:300]}")
        start = end


def tmsh(req, command):
    st, body = req("POST", "/mgmt/tm/util/bash",
                    data=json.dumps({"command": "run", "utilCmdArgs": f'-c "{command}"'}).encode())
    if st != 200:
        die(f"tmsh command failed: HTTP {st} {body[:400]}\ncommand: {command}")
    result = json.loads(body).get("commandResult", "")
    if "01070" in result or "error" in result.lower():
        print(f"WARNING: tmsh may have failed: {result}", file=sys.stderr)
    return result


def install_cert(bigip_host, bigip_user, bigip_pass, cert_name, cert_pem, key_pem, attach_profile=None):
    """Upload + tmsh-install a cert/key pair as sys crypto objects named
    cert_name, optionally attaching them to a client-ssl profile's
    cert-key-chain. Returns nothing; dies on failure.

    Re-installing over an existing cert_name is NOT reliable with a bare
    `tmsh install ... from-local-file` (confirmed empirically: re-running
    against a name already attached to a profile silently produced a
    mismatched cert/key pair). So this always does a clean delete-then-
    install: detach the profile from the old objects first (BIG-IP won't
    let you delete a cert/key that's in use), delete them, then install
    fresh and reattach."""
    # cert_name and attach_profile are interpolated into tmsh strings that run
    # as root on the device; refuse anything that is not a plain object name.
    require_bigip_name(cert_name, "certificate name")
    if attach_profile is not None:
        require_bigip_name(attach_profile, "profile name")

    req = bigip_client(bigip_host, bigip_user, bigip_pass)

    if attach_profile:
        tmsh(req, f"tmsh modify ltm profile client-ssl {attach_profile} "
                  f"cert-key-chain replace-all-with {{ default {{ cert default.crt key default.key }} }}")
    tmsh(req, f"tmsh delete sys crypto key {cert_name}")
    tmsh(req, f"tmsh delete sys crypto cert {cert_name}")

    cert_file = f"{cert_name}.crt"
    key_file = f"{cert_name}.key"
    upload_to_bigip(req, cert_file, cert_pem.encode())
    upload_to_bigip(req, key_file, key_pem.encode())

    print(tmsh(req, f"tmsh install sys crypto cert {cert_name} from-local-file "
                     f"/var/config/rest/downloads/{cert_file}"))
    print(tmsh(req, f"tmsh install sys crypto key {cert_name} from-local-file "
                     f"/var/config/rest/downloads/{key_file}"))

    if attach_profile:
        print(tmsh(req, f"tmsh modify ltm profile client-ssl {attach_profile} "
                         f"cert-key-chain replace-all-with {{ default {{ cert {cert_name} "
                         f"key {cert_name} }} }}"))
        print(f"Attached to client-ssl profile '{attach_profile}'.")

    print(f"Installed sys crypto cert/key '{cert_name}' on {bigip_host}.")
