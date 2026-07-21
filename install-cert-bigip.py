#!/usr/bin/env python3
"""Install a PEM cert/key pair on a BIG-IP as named sys crypto objects,
optionally attaching them to a client-ssl profile. Used directly for the
EST proxy VS's own bootstrap certificate (issued straight from OpenBao,
not via EST -- see README "chicken-and-egg" note), and by quickstart.sh.

For getting a cert onto a BIG-IP via an actual EST enrollment instead,
use bigip-est-enroll.py.

Usage:
  install-cert-bigip.py --bigip-host <ip> --bigip-user admin --bigip-pass '...' \\
    --cert-name <name> --cert-file vs.crt --key-file vs.key \\
    [--attach-profile est-clientssl]
"""
import argparse

from bigip_lib import install_cert


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bigip-host", required=True)
    p.add_argument("--bigip-user", required=True)
    p.add_argument("--bigip-pass", required=True)
    p.add_argument("--cert-name", required=True, help="sys crypto cert/key object name on the BIG-IP")
    p.add_argument("--cert-file", required=True, help="PEM certificate file")
    p.add_argument("--key-file", required=True, help="PEM private key file")
    p.add_argument("--attach-profile", help="also attach the cert/key to this client-ssl profile")
    args = p.parse_args()

    with open(args.cert_file) as f:
        cert_pem = f.read()
    with open(args.key_file) as f:
        key_pem = f.read()

    install_cert(args.bigip_host, args.bigip_user, args.bigip_pass,
                 args.cert_name, cert_pem, key_pem, args.attach_profile)


if __name__ == "__main__":
    main()
