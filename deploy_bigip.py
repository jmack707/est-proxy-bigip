#!/usr/bin/env python3
"""One-off deploy of the EST proxy scenario onto a BIG-IP via iControl REST.
Usage: deploy_bigip.py <mgmt-host> <user> <pw> <irule_file>

Edit the CONFIG block below for your environment (pool member, VS
destination/VLAN) before running.
"""
import sys, json, base64, ssl, urllib.request, urllib.error

# --- CONFIG: adjust for your environment ---
POOL_MEMBER = "10.1.30.20:8085"       # backend est_shim.py address:port
VS_DESTINATION = "10.1.10.30:8443"    # BIG-IP VS listener
VS_VLAN = "/Common/external"          # VLAN the VS listens on
# --------------------------------------------

host, user, pw, irule_file = sys.argv[1:5]
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
auth = base64.b64encode(f"{user}:{pw}".encode()).decode()

def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"https://{host}{path}", data=data, method=method,
                                headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, context=ctx, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def ensure(method_create_path, body, name_for_msg):
    st, b = req("POST", method_create_path, body)
    if st in (200, 201):
        print(f"created: {name_for_msg}")
    elif st == 409 or (st == 400 and b"already exists" in b):
        print(f"already exists (ok): {name_for_msg}")
    else:
        print(f"FAILED {name_for_msg}: HTTP {st} {b[:300]}", file=sys.stderr)
        sys.exit(1)

with open(irule_file) as f:
    irule_src = f.read()

# 1. Pool
ensure("/mgmt/tm/ltm/pool", {
    "name": "est-backend-pool",
    "monitor": "tcp",
    "members": [{"name": POOL_MEMBER, "address": POOL_MEMBER.split(":")[0]}],
}, "ltm pool est-backend-pool")

# 2. Client SSL profile — request (not require) a client cert so simpleenroll/cacerts
# work without one, and simplereenroll's cert-count check in the iRule enforces its own case.
ensure("/mgmt/tm/ltm/profile/client-ssl", {
    "name": "est-clientssl",
    "defaultsFrom": "/Common/clientssl",
    "peerCertMode": "request",
}, "ltm profile client-ssl est-clientssl")

# 3. iRule
ensure("/mgmt/tm/ltm/rule", {
    "name": "est_proxy",
    "apiAnonymous": irule_src,
}, "ltm rule est_proxy")

# 4. Virtual server
ensure("/mgmt/tm/ltm/virtual", {
    "name": "est-proxy-vs",
    "destination": f"/Common/{VS_DESTINATION}",
    "ipProtocol": "tcp",
    "pool": "/Common/est-backend-pool",
    "sourceAddressTranslation": {"type": "automap"},
    "profiles": [
        {"name": "/Common/est-clientssl", "context": "clientside"},
        {"name": "/Common/http"},
        {"name": "/Common/tcp"},
    ],
    "rules": ["/Common/est_proxy"],
    "vlansEnabled": True,
    "vlans": [VS_VLAN],
}, "ltm virtual est-proxy-vs")

print("done")
