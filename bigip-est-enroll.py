#!/usr/bin/env python3
"""Enroll a certificate via EST (using the real libest `estclient`) and
install it on a BIG-IP -- since TMOS has no native EST client (verified
against F5 docs: Certificate Order Manager only supports specific commercial
CA vendor APIs, ACMEv2 was BIG-IP 21.1's headline new cert-automation
feature, no EST anywhere), this script is the bridge: it does the EST
protocol exchange externally with the real client, then pushes the result
onto the device the same way a human would with tmsh.

Requires the `estclient` binary (Debian/Ubuntu: apt install libest-utils)
and network access to both the EST server and the BIG-IP's iControl REST.

Usage (first enrollment):
  bigip-est-enroll.py enroll \\
    --est-host est.example.com --est-port 8443 --est-cacert ca-chain.pem \\
    --common-name bigip-a.example.com \\
    --bigip-host 10.1.1.5 --bigip-user admin --bigip-pass '...' \\
    --cert-name bigip-a-cert [--attach-profile est-clientssl]

Usage (renew an existing enrollment -- needs the PEM cert/key this tool
saved on a prior run, via --save-dir):
  bigip-est-enroll.py renew \\
    --est-host est.example.com --est-port 8443 --est-cacert ca-chain.pem \\
    --existing-cert saved/bigip-a-cert.pem --existing-key saved/bigip-a-cert.key \\
    --bigip-host 10.1.1.5 --bigip-user admin --bigip-pass '...' \\
    --cert-name bigip-a-cert [--attach-profile est-clientssl]
"""
import argparse
import base64
import os
import shutil
import subprocess
import sys
import tempfile

from bigip_lib import die, install_cert


def run_estclient(args, cacert, env_extra=None):
    env = dict(os.environ)
    env["EST_OPENSSL_CACERT"] = cacert
    env.update(env_extra or {})
    proc = subprocess.run(["estclient", *args], capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        die(f"estclient failed (rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout + proc.stderr


def pkcs7_to_pem(pkcs7_b64_file):
    """estclient writes response bodies as base64 text under names like
    cert-0-0.pkcs7 regardless of the naming -- decode + unwrap to a plain
    PEM cert."""
    with open(pkcs7_b64_file, "rb") as f:
        der = base64.b64decode(f.read())
    proc = subprocess.run(
        ["openssl", "pkcs7", "-inform", "DER", "-print_certs"],
        input=der, capture_output=True, check=True,
    )
    return proc.stdout.decode()


def find_one(directory, prefix):
    matches = [f for f in os.listdir(directory) if f.startswith(prefix)]
    if not matches:
        die(f"no file starting with '{prefix}' found in {directory} (estclient output missing?)")
    return os.path.join(directory, matches[0])


def enroll(args):
    with tempfile.TemporaryDirectory() as outdir:
        if args.mode == "enroll":
            run_estclient(
                ["-e", "-s", args.est_host, "-p", str(args.est_port), "-o", outdir,
                 "--common-name", args.common_name,
                 *(["-u", args.ldap_user, "-h", args.ldap_password] if args.ldap_user else [])],
                args.est_cacert,
            )
        else:  # renew
            run_estclient(
                ["-r", "-s", args.est_host, "-p", str(args.est_port), "-o", outdir,
                 "-c", args.existing_cert, "-k", args.existing_key],
                args.est_cacert,
            )

        cert_pem = pkcs7_to_pem(find_one(outdir, "cert-"))
        if args.mode == "enroll":
            key_path = find_one(outdir, "key-")
            with open(key_path) as f:
                key_pem = f.read()
        else:
            with open(args.existing_key) as f:
                key_pem = f.read()

    if args.save_dir:
        os.makedirs(args.save_dir, exist_ok=True)
        cert_out = os.path.join(args.save_dir, f"{args.cert_name}.pem")
        key_out = os.path.join(args.save_dir, f"{args.cert_name}.key")
        with open(cert_out, "w") as f:
            f.write(cert_pem)
        with open(key_out, "w") as f:
            f.write(key_pem)
        print(f"Saved {cert_out} / {key_out} (pass these as --existing-cert/--existing-key next time to renew)")

    return cert_pem, key_pem


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["enroll", "renew"])
    p.add_argument("--est-host", required=True)
    p.add_argument("--est-port", type=int, default=8443)
    p.add_argument("--est-cacert", required=True, help="CA chain PEM to trust the EST server (EST_OPENSSL_CACERT)")
    p.add_argument("--common-name", help="required for 'enroll'")
    p.add_argument("--existing-cert", help="required for 'renew' -- PEM cert from a prior enroll/renew")
    p.add_argument("--existing-key", help="required for 'renew'")
    p.add_argument("--ldap-user", help="if the EST server gates simpleenroll on LDAP (HTTP Basic)")
    p.add_argument("--ldap-password")
    p.add_argument("--save-dir", help="save the resulting PEM cert/key here for a future 'renew'")
    p.add_argument("--bigip-host", required=True)
    p.add_argument("--bigip-user", required=True)
    p.add_argument("--bigip-pass", required=True)
    p.add_argument("--cert-name", required=True, help="sys crypto cert/key object name on the BIG-IP")
    p.add_argument("--attach-profile", help="also attach the new cert/key to this client-ssl profile")
    args = p.parse_args()

    if args.mode == "enroll" and not args.common_name:
        die("--common-name is required for 'enroll'")
    if args.mode == "renew" and not (args.existing_cert and args.existing_key):
        die("--existing-cert and --existing-key are required for 'renew'")
    if not shutil.which("estclient"):
        die("estclient not found -- install it (Debian/Ubuntu: apt install libest-utils)")

    cert_pem, key_pem = enroll(args)
    install_cert(args.bigip_host, args.bigip_user, args.bigip_pass,
                 args.cert_name, cert_pem, key_pem, args.attach_profile)


if __name__ == "__main__":
    main()
