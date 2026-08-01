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
import os
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
    p.add_argument("--proxy-secret", default="",
                   help="shared secret the iRule presents to the backend as "
                        "X-EST-Proxy-Secret; must equal the shim's EST_PROXY_SECRET")
    args = p.parse_args()

    if ":" not in args.pool_member:
        die(f"--pool-member must be HOST:PORT, got '{args.pool_member}'")
    if ":" not in args.vs_destination:
        die(f"--vs-destination must be IP:PORT, got '{args.vs_destination}'")

    # Verification is off unless BIGIP_CA_FILE points at a bundle that signs the
    # management certificate. The management credentials cross this channel, so
    # verify it in production; a fresh BIG-IP is self-signed, hence the default.
    ca_file = os.environ.get("BIGIP_CA_FILE", "")
    if ca_file:
        ctx = ssl.create_default_context(cafile=ca_file)
    else:
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

    def obj_path(base, name):
        """REST path of an existing object: 'est_proxy' -> '<base>/~Common~est_proxy'."""
        return f"{base}/{qualify(name).replace('/', '~')}"

    def ensure(create_path, body, name_for_msg, converge=None):
        """Create the object; if it already exists, optionally reconcile it.

        A POST that lands on an existing object is reported and skipped. That
        is right for objects the deploy creates but does not own the contents
        of, and wrong for the two it does: the pool's members and the iRule's
        body. Skipping there leaves a pool with no member, or an iRule that
        never picks up a new revision -- and because the skip is reported as
        success, every subsequent run looks fine while the deployment stays
        broken. `converge` is (path, patch_body) applied on that branch.
        """
        st, b = req("POST", create_path, body)
        if st in (200, 201):
            print(f"created: {name_for_msg}")
            return
        if st == 409 or (st == 400 and b"already exists" in b):
            if converge is None:
                print(f"already exists (ok): {name_for_msg}")
                return
            path, patch = converge
            pst, pb = req("PATCH", path, patch)
            if pst in (200, 201):
                print(f"already exists, reconciled: {name_for_msg}")
            else:
                die(f"{name_for_msg}: exists, but reconciling it failed: HTTP {pst} {pb[:300]}")
            return
        die(f"{name_for_msg}: HTTP {st} {b[:300]}")

    with open(args.irule_file) as f:
        irule_src = f.read()

    # Substitute the shared secret the iRule presents to the backend. The rule
    # ships with an empty value, which disables the header; the backend then has
    # only network isolation distinguishing proxied traffic from direct traffic.
    if args.proxy_secret:
        marker = 'set static::est_proxy_secret ""'
        if marker not in irule_src:
            die(f"--proxy-secret given but {args.irule_file} has no '{marker}' line to substitute")
        if '"' in args.proxy_secret or "\\" in args.proxy_secret:
            die("--proxy-secret must not contain quotes or backslashes (it is inlined into Tcl)")
        irule_src = irule_src.replace(
            marker, f'set static::est_proxy_secret "{args.proxy_secret}"', 1)

    # 1. Pool. Members are reconciled on an existing pool: a pool that already
    # exists with no member -- from a partial earlier run, or pre-seeded -- would
    # otherwise stay memberless while the deploy reported success.
    pool_members = [{"name": args.pool_member, "address": args.pool_member.split(":")[0]}]
    ensure("/mgmt/tm/ltm/pool", {
        "name": args.pool_name,
        "monitor": "tcp",
        "members": pool_members,
    }, f"ltm pool {args.pool_name}",
        converge=(obj_path("/mgmt/tm/ltm/pool", args.pool_name), {"members": pool_members}))

    # 2. Client SSL profile — request (not require) a client cert so simpleenroll/cacerts
    # work without one, and simplereenroll's cert-count check in the iRule enforces its own case.
    # Deliberately not reconciled: install-cert-bigip.py owns this profile's
    # cert-key-chain, and re-running the deploy must not disturb the certificate
    # the virtual server is currently serving.
    ensure("/mgmt/tm/ltm/profile/client-ssl", {
        "name": args.clientssl_profile,
        "defaultsFrom": "/Common/clientssl",
        "peerCertMode": "request",
    }, f"ltm profile client-ssl {args.clientssl_profile}")

    # 3. iRule. Reconciled on an existing rule, so re-running the deploy after
    # editing the iRule actually uploads the new body -- which is what the
    # upgrade procedure relies on.
    ensure("/mgmt/tm/ltm/rule", {
        "name": args.irule_name,
        "apiAnonymous": irule_src,
    }, f"ltm rule {args.irule_name}",
        converge=(obj_path("/mgmt/tm/ltm/rule", args.irule_name), {"apiAnonymous": irule_src}))

    # 4. Virtual server. Also not reconciled: changing the listener or the
    # profile set under live traffic is a decision for an operator, not a
    # side effect of re-running the deploy. Change it deliberately, or remove
    # the virtual server and re-run.
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
