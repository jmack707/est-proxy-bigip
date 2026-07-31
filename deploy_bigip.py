#!/usr/bin/env python3
"""One-off deploy of the EST proxy scenario onto a BIG-IP via iControl REST.

Usage:
  deploy_bigip.py <mgmt-host> <user> <pw> <irule_file> \\
    --pool-member <host:port> --vs-destination <ip:port> [--vs-vlan </Common/name>]

Example:
  deploy_bigip.py 10.1.1.5 admin '...' est_proxy.irule.tcl \\
    --pool-member 10.1.30.20:8085 --vs-destination 10.1.10.30:8443
"""
import argparse
import base64
import json
import ssl
import sys
import urllib.error
import urllib.request


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def qualify(name):
    """Return an object name in /Partition/name form.

    Names may be given bare ("clientssl") or already qualified
    ("/Common/clientssl"). Prefixing unconditionally turns the second form into
    "/Common/Common/clientssl", which the REST API rejects with a 404 that names
    a profile nobody asked for.
    """
    return name if name.startswith("/") else f"/Common/{name}"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("host", help="BIG-IP management host/IP")
    p.add_argument("user")
    p.add_argument("password")
    p.add_argument("irule_file")
    p.add_argument("--pool-member", required=True, metavar="HOST:PORT",
                    help="backend est_shim.py address:port")
    p.add_argument("--vs-destination", required=True, metavar="IP:PORT",
                    help="BIG-IP virtual server listener")
    p.add_argument("--vs-vlan", default="/Common/external", metavar="/Partition/name",
                    help="VLAN the VS listens on (default: /Common/external)")
    p.add_argument("--pool-name", default="est-backend-pool")
    p.add_argument("--clientssl-profile", default="est-clientssl")
    p.add_argument("--irule-name", default="est_proxy")
    p.add_argument("--vs-name", default="est-proxy-vs")
    args = p.parse_args()

    if ":" not in args.pool_member:
        die(f"--pool-member must be HOST:PORT, got '{args.pool_member}'")
    if ":" not in args.vs_destination:
        die(f"--vs-destination must be IP:PORT, got '{args.vs_destination}'")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    auth = base64.b64encode(f"{args.user}:{args.password}".encode()).decode()

    def req(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"https://{args.host}{path}", data=data, method=method,
                                    headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, context=ctx, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def ensure(create_path, body, name_for_msg):
        st, b = req("POST", create_path, body)
        if st in (200, 201):
            print(f"created: {name_for_msg}")
        elif st == 409 or (st == 400 and b"already exists" in b):
            print(f"already exists (ok): {name_for_msg}")
        else:
            die(f"{name_for_msg}: HTTP {st} {b[:300]}")

    with open(args.irule_file) as f:
        irule_src = f.read()

    # 1. Pool
    ensure("/mgmt/tm/ltm/pool", {
        "name": args.pool_name,
        "monitor": "tcp",
        "members": [{"name": args.pool_member, "address": args.pool_member.split(":")[0]}],
    }, f"ltm pool {args.pool_name}")

    # 2. Client SSL profile — request (not require) a client cert so simpleenroll/cacerts
    # work without one, and simplereenroll's cert-count check in the iRule enforces its own case.
    ensure("/mgmt/tm/ltm/profile/client-ssl", {
        "name": args.clientssl_profile,
        "defaultsFrom": "/Common/clientssl",
        "peerCertMode": "request",
    }, f"ltm profile client-ssl {args.clientssl_profile}")

    # 3. iRule
    ensure("/mgmt/tm/ltm/rule", {
        "name": args.irule_name,
        "apiAnonymous": irule_src,
    }, f"ltm rule {args.irule_name}")

    # 4. Virtual server
    ensure("/mgmt/tm/ltm/virtual", {
        "name": args.vs_name,
        "destination": qualify(args.vs_destination),
        "ipProtocol": "tcp",
        "pool": qualify(args.pool_name),
        "sourceAddressTranslation": {"type": "automap"},
        "profiles": [
            {"name": qualify(args.clientssl_profile), "context": "clientside"},
            {"name": "/Common/http"},
            {"name": "/Common/tcp"},
        ],
        "rules": [qualify(args.irule_name)],
        "vlansEnabled": True,
        "vlans": [qualify(args.vs_vlan)],
    }, f"ltm virtual {args.vs_name}")

    print("done")


if __name__ == "__main__":
    main()
