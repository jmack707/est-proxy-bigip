#!/usr/bin/env python3
"""Minimal RFC 7030 EST server, backed by OpenBao/Vault pki, with optional
LDAP (FreeIPA or Active Directory) client authentication.

Speaks the subset of EST BIG-IP's est_proxy iRule proxies: cacerts,
simpleenroll, simplereenroll, serverkeygen. TLS is terminated at the BIG-IP
VS; this listens plain HTTP behind it. Only non-stdlib dependency is
`ldap3` (pure Python, no C bindings), and only imported when LDAP_ENABLED.
Uses the system openssl binary for PKCS#7 degenerate-certs-only packaging,
which cryptography's pkcs7 module can't produce.
"""
import base64
import http.server
import json
import os
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

BAO_ADDR = os.environ.get("BAO_ADDR", "https://127.0.0.1:8200")
BAO_ROLE_ID = os.environ["BAO_ROLE_ID"]
BAO_SECRET_ID = os.environ["BAO_SECRET_ID"]
PKI_MOUNT = os.environ.get("PKI_MOUNT", "pki_int")
PKI_ROLE = os.environ.get("PKI_ROLE", "example-dot-com")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8085"))

# --- optional LDAP (FreeIPA / Active Directory) client authentication ---
# Gates simpleenroll (not simplereenroll, which already authenticates via the
# client's existing TLS cert per RFC 7030 -- that's the protocol's own
# reenroll trust chain, not something LDAP needs to duplicate).
LDAP_ENABLED = os.environ.get("LDAP_ENABLED", "false").lower() == "true"
LDAP_URI = os.environ.get("LDAP_URI", "ldaps://127.0.0.1:636")
# {username} is substituted with the HTTP-Basic-Auth username. Examples:
#   FreeIPA: uid={username},cn=users,cn=accounts,dc=example,dc=com
#   AD:      {username}@example.com   (UPN bind -- simplest against AD)
#   AD alt:  CN={username},CN=Users,DC=example,DC=com
LDAP_BIND_DN_TEMPLATE = os.environ.get("LDAP_BIND_DN_TEMPLATE", "{username}")
LDAP_START_TLS = os.environ.get("LDAP_START_TLS", "false").lower() == "true"
# Which EST operations require LDAP auth (comma-separated).
LDAP_REQUIRE_OPS = set(os.environ.get("LDAP_REQUIRE_OPS", "simpleenroll").split(","))
# Reject if the authenticated username doesn't match the CSR's CN -- stops
# user A, once authenticated, from requesting a cert identifying user B.
LDAP_ENFORCE_CN_MATCH = os.environ.get("LDAP_ENFORCE_CN_MATCH", "true").lower() == "true"

_ssl_ctx = ssl._create_unverified_context()  # OpenBao uses the Lab CA; shim trusts it by design (internal-only listener)


def ldap_authenticate(username, password):
    """Bind as the user against FreeIPA or AD. Returns (ok, detail)."""
    import ldap3  # imported lazily so LDAP_ENABLED=false needs no extra dependency
    bind_dn = LDAP_BIND_DN_TEMPLATE.format(username=username)
    server = ldap3.Server(LDAP_URI, use_ssl=LDAP_URI.startswith("ldaps://"))
    try:
        conn = ldap3.Connection(server, user=bind_dn, password=password)
        if LDAP_START_TLS:
            conn.open()
            if not conn.start_tls():
                return False, "STARTTLS negotiation failed"
        if not conn.bind():
            return False, f"bind failed: {conn.result.get('description', 'unknown')}"
        conn.unbind()
        return True, "ok"
    except ldap3.core.exceptions.LDAPException as e:
        return False, f"LDAP error: {e}"


def csr_common_name(csr_pem):
    out = subprocess.run(
        ["openssl", "req", "-noout", "-subject", "-nameopt", "sep_multiline,utf8"],
        input=csr_pem.encode(), capture_output=True, check=True,
    )
    for line in out.stdout.decode().splitlines():
        line = line.strip()
        if line.startswith("CN="):
            return line[3:]
    return None


def bao_request(method, path, token=None, body=None):
    url = f"{BAO_ADDR}/v1/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("X-Vault-Token", token)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as resp:
        return json.loads(resp.read())


def bao_login():
    resp = bao_request("POST", "auth/approle/login", body={
        "role_id": BAO_ROLE_ID, "secret_id": BAO_SECRET_ID,
    })
    return resp["auth"]["client_token"]


def bao_raw_get(path):
    """ca_chain is served as raw PEM text, not JSON."""
    req = urllib.request.Request(f"{BAO_ADDR}/v1/{path}", method="GET")
    with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as resp:
        return resp.read().decode()


def pkcs7_degenerate(pem_certs):
    """Wrap one or more PEM certs into a degenerate (certs-only) PKCS#7,
    per RFC 7030 sec 4.1.3 / 4.2.3. openssl crl2pkcs7 -nocrl does exactly this."""
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
        f.write(pem_certs)
        certfile = f.name
    try:
        out = subprocess.run(
            ["openssl", "crl2pkcs7", "-nocrl", "-certfile", certfile, "-outform", "DER"],
            capture_output=True, check=True,
        )
        return out.stdout
    finally:
        os.unlink(certfile)


def csr_pem_from_der_b64(b64_body):
    der = base64.b64decode(b64_body)
    out = subprocess.run(
        ["openssl", "req", "-inform", "DER", "-outform", "PEM"],
        input=der, capture_output=True, check=True,
    )
    return out.stdout.decode()


class ESTHandler(http.server.BaseHTTPRequestHandler):
    server_version = "est-shim/0.1 (lab test, OpenBao-backed)"
    protocol_version = "HTTP/1.0"  # libest's HTTP status-line parser only accepts HTTP/1.0

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, content_type, body, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _est_path(self):
        # strip query string, expect /.well-known/est/[<label>/]<op>
        path = self.path.split("?", 1)[0]
        prefix = "/.well-known/est/"
        if not path.startswith(prefix):
            return None
        rest = path[len(prefix):].strip("/").split("/")
        ops = {"cacerts", "simpleenroll", "simplereenroll", "serverkeygen", "csrattrs"}
        if rest and rest[0] in ops:
            return rest[0], ""
        if len(rest) >= 2 and rest[1] in ops:
            return rest[1], rest[0]
        return None

    def do_GET(self):
        parsed = self._est_path()
        if not parsed:
            self._send(404, "text/plain", b"not found")
            return
        op, label = parsed
        if op == "cacerts":
            try:
                pem_chain = bao_raw_get(f"{PKI_MOUNT}/ca_chain")
            except urllib.error.HTTPError as e:
                self._send(502, "text/plain", f"OpenBao ca_chain error: {e}".encode())
                return
            self._send(200, "application/pkcs7-mime; smime-type=certs-only",
                       base64.encodebytes(pkcs7_degenerate(pem_chain)),
                       {"Content-Transfer-Encoding": "base64"})
            return
        if op == "csrattrs":
            self._send(200, "application/csrattrs", b"")
            return
        self._send(404, "text/plain", b"unsupported GET op")

    def do_POST(self):
        parsed = self._est_path()
        if not parsed:
            self._send(404, "text/plain", b"not found")
            return
        op, label = parsed
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        client_cert_present = bool(self.headers.get("X-SSL-Client-Cert"))

        if op == "simplereenroll" and not client_cert_present:
            self._send(401, "text/plain", b"reenroll requires client cert (X-SSL-Client-Cert missing)")
            return

        ldap_username = None
        if LDAP_ENABLED and op in LDAP_REQUIRE_OPS:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                self._send(401, "text/plain", b"LDAP auth required (HTTP Basic)",
                           {"WWW-Authenticate": 'Basic realm="EST"'})
                return
            try:
                ldap_username, ldap_password = base64.b64decode(auth[6:]).decode().split(":", 1)
            except Exception:
                self._send(400, "text/plain", b"malformed Authorization header")
                return
            ok, detail = ldap_authenticate(ldap_username, ldap_password)
            if not ok:
                self._send(403, "text/plain", f"LDAP authentication failed: {detail}".encode())
                return

        try:
            token = bao_login()
        except Exception as e:
            self._send(502, "text/plain", f"OpenBao AppRole login failed: {e}".encode())
            return

        if op in ("simpleenroll", "simplereenroll"):
            try:
                csr_pem = csr_pem_from_der_b64(body)
            except subprocess.CalledProcessError as e:
                self._send(400, "text/plain", f"bad CSR: {e.stderr}".encode())
                return
            if ldap_username and LDAP_ENFORCE_CN_MATCH:
                cn = csr_common_name(csr_pem)
                if cn != ldap_username:
                    self._send(403, "text/plain",
                               f"CSR CN '{cn}' does not match authenticated LDAP user '{ldap_username}'".encode())
                    return
            try:
                signed = bao_request("POST", f"{PKI_MOUNT}/sign/{PKI_ROLE}", token=token,
                                      body={"csr": csr_pem})
            except urllib.error.HTTPError as e:
                self._send(502, "text/plain", f"OpenBao sign error: {e.read()}".encode())
                return
            cert_pem = signed["data"]["certificate"]
            self._send(200, "application/pkcs7-mime; smime-type=certs-only",
                       base64.encodebytes(pkcs7_degenerate(cert_pem)),
                       {"Content-Transfer-Encoding": "base64"})
            return

        if op == "serverkeygen":
            cn = f"est-serverkeygen.{os.environ.get('DOMAIN', 'example.com')}"
            try:
                issued = bao_request("POST", f"{PKI_MOUNT}/issue/{PKI_ROLE}", token=token,
                                      body={"common_name": cn})
            except urllib.error.HTTPError as e:
                self._send(502, "text/plain", f"OpenBao issue error: {e.read()}".encode())
                return
            cert_pem = issued["data"]["certificate"]
            key_pem = issued["data"]["private_key"]
            pkcs7 = pkcs7_degenerate(cert_pem)
            # multipart/mixed: pkcs7-mime cert part + the server-generated key
            boundary = "estshimboundary"
            parts = (
                f"--{boundary}\r\nContent-Type: application/pkcs7-mime; smime-type=certs-only\r\n"
                f"Content-Transfer-Encoding: base64\r\n\r\n{base64.encodebytes(pkcs7).decode()}\r\n"
                f"--{boundary}\r\nContent-Type: application/pkcs8\r\nContent-Transfer-Encoding: base64\r\n\r\n"
                f"{base64.encodebytes(key_pem.encode()).decode()}\r\n--{boundary}--\r\n"
            ).encode()
            self._send(200, f"multipart/mixed; boundary={boundary}", parts)
            return

        self._send(404, "text/plain", b"unsupported POST op")


if __name__ == "__main__":
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), ESTHandler)
    print(f"est-shim listening on :{LISTEN_PORT}, OpenBao={BAO_ADDR}, role={PKI_MOUNT}/{PKI_ROLE}")
    srv.serve_forever()
