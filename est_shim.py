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
import hmac
import http.server
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
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
# Which EST operations require LDAP auth (comma-separated). serverkeygen is in
# the default set: it issues a certificate too, so leaving it out meant enabling
# the gate still left serverkeygen open to anyone who could reach the endpoint.
# simplereenroll stays out deliberately -- it authenticates with the existing
# certificate over TLS, verified separately (ADR-0008).
LDAP_REQUIRE_OPS = set(os.environ.get("LDAP_REQUIRE_OPS", "simpleenroll,serverkeygen").split(","))
# Reject if the authenticated username doesn't match the CSR's CN -- stops
# user A, once authenticated, from requesting a cert identifying user B.
LDAP_ENFORCE_CN_MATCH = os.environ.get("LDAP_ENFORCE_CN_MATCH", "true").lower() == "true"
# CA bundle used to verify the directory's TLS certificate. Unset means ldap3's
# default of no verification, which encrypts without authenticating the server
# and leaves directory passwords exposed to anyone on the path.
LDAP_CA_FILE = os.environ.get("LDAP_CA_FILE", "")

# --- proxy trust ---
# The iRule is what makes X-SSL-Client-* trustworthy, and it is only in the path
# for traffic that arrives through the virtual server. Anything that can reach
# LISTEN_PORT directly can set those headers itself. This shared secret, injected
# by the iRule, is what distinguishes "came through the BIG-IP" from "reached the
# port". Unset means that distinction is not made and only network isolation
# protects the listener.
EST_PROXY_SECRET = os.environ.get("EST_PROXY_SECRET", "")
# Verify the client certificate the iRule forwards, rather than trusting that a
# header exists. Turning this off restores the pre-hardening behaviour, in which
# any request carrying the header is treated as an authenticated reenrolment.
REENROLL_VERIFY_CERT = os.environ.get("REENROLL_VERIFY_CERT", "true").lower() == "true"

# --- authentication rate limiting ---
# Every gated enrolment is a live directory bind, so an unthrottled endpoint is
# both a password-guessing channel and a way to trip account lockout on real
# accounts. Counted per username, in this process only.
AUTH_MAX_FAILURES = int(os.environ.get("AUTH_MAX_FAILURES", "5"))
AUTH_WINDOW_SECONDS = int(os.environ.get("AUTH_WINDOW_SECONDS", "300"))

_ssl_ctx = ssl._create_unverified_context()  # OpenBao uses the Lab CA; shim trusts it by design (internal-only listener)

_auth_failures = {}
_auth_lock = threading.Lock()


def auth_throttled(key):
    """True when this key has failed too often inside the window."""
    if AUTH_MAX_FAILURES <= 0:
        return False
    now = time.monotonic()
    with _auth_lock:
        recent = [t for t in _auth_failures.get(key, []) if now - t < AUTH_WINDOW_SECONDS]
        _auth_failures[key] = recent
        return len(recent) >= AUTH_MAX_FAILURES


def record_auth_failure(key):
    now = time.monotonic()
    with _auth_lock:
        _auth_failures.setdefault(key, []).append(now)


def clear_auth_failures(key):
    with _auth_lock:
        _auth_failures.pop(key, None)


_ca_chain_cache = {"pem": None}


def ca_chain_pem(refresh=False):
    """The issuing chain, used to verify client certificates on reenrolment."""
    if refresh or _ca_chain_cache["pem"] is None:
        _ca_chain_cache["pem"] = bao_raw_get(f"{PKI_MOUNT}/ca_chain")
    return _ca_chain_cache["pem"]


def ldap_authenticate(username, password):
    """Bind as the user against FreeIPA or AD. Returns (ok, detail)."""
    import ldap3  # imported lazily so LDAP_ENABLED=false needs no extra dependency
    bind_dn = LDAP_BIND_DN_TEMPLATE.format(username=username)
    tls = None
    if LDAP_CA_FILE:
        tls = ldap3.Tls(ca_certs_file=LDAP_CA_FILE, validate=ssl.CERT_REQUIRED)
    server = ldap3.Server(LDAP_URI, use_ssl=LDAP_URI.startswith("ldaps://"), tls=tls)
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


def csr_san_dns_names(csr_pem):
    """dNSName SANs requested by the CSR.

    The PKI role signs with use_csr_sans on by default, so anything here lands
    in the issued certificate. Since TLS peers validate against SANs rather than
    the CN, checking only the CN leaves the name-binding unenforced.
    """
    out = subprocess.run(
        ["openssl", "req", "-noout", "-text"],
        input=csr_pem.encode(), capture_output=True, check=True,
    )
    names = []
    lines = out.stdout.decode().splitlines()
    for i, line in enumerate(lines):
        if "Subject Alternative Name" in line and i + 1 < len(lines):
            for entry in lines[i + 1].split(","):
                entry = entry.strip()
                if entry.startswith("DNS:"):
                    names.append(entry[4:].strip())
    return names


def x509_common_name(cert_pem):
    out = subprocess.run(
        ["openssl", "x509", "-noout", "-subject", "-nameopt", "sep_multiline,utf8"],
        input=cert_pem.encode(), capture_output=True, check=True,
    )
    for line in out.stdout.decode().splitlines():
        line = line.strip()
        if line.startswith("CN="):
            return line[3:]
    return None


def client_cert_from_header(raw):
    """The iRule sends URI-encoded PEM; tolerate an already-decoded value."""
    if not raw:
        return None
    pem = urllib.parse.unquote(raw)
    if "BEGIN CERTIFICATE" not in pem:
        return None
    return pem


def verify_client_cert(cert_pem):
    """Verify against the issuing chain. Returns (ok, detail).

    openssl verify covers signature, chain and validity dates in one pass, so an
    expired or self-minted certificate fails here rather than being accepted on
    the strength of the header existing.
    """
    chain_file = cert_file = None
    try:
        for attempt in (0, 1):
            try:
                chain = ca_chain_pem(refresh=bool(attempt))
            except Exception as e:
                return False, f"cannot fetch issuing chain: {e}"
            with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
                f.write(chain)
                chain_file = f.name
            with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
                f.write(cert_pem)
                cert_file = f.name
            out = subprocess.run(
                ["openssl", "verify", "-CAfile", chain_file, cert_file],
                capture_output=True,
            )
            if out.returncode == 0:
                return True, "ok"
            detail = (out.stderr or out.stdout).decode().strip().splitlines()
            detail = detail[-1] if detail else "verification failed"
            # A rotated intermediate looks exactly like a bad certificate; retry
            # once against a freshly fetched chain before rejecting.
            if attempt == 0 and "unable to get local issuer" in detail:
                continue
            return False, detail
        return False, "verification failed"
    finally:
        for path in (chain_file, cert_file):
            if path and os.path.exists(path):
                os.unlink(path)


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
            except urllib.error.URLError as e:
                # HTTPError covers "the backend answered with an error status".
                # A backend that is down, or a wrong BAO_ADDR, raises URLError --
                # HTTPError's parent -- which would otherwise escape and kill the
                # handler, dropping the connection with no response at all.
                self._send(502, "text/plain",
                           f"OpenBao unreachable at {BAO_ADDR}: {e.reason}".encode())
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

        # Did this arrive through the iRule, or straight at the port? Without
        # this the X-SSL-Client-* headers below are attacker-supplied.
        if EST_PROXY_SECRET:
            presented = self.headers.get("X-EST-Proxy-Secret", "")
            if not hmac.compare_digest(presented, EST_PROXY_SECRET):
                self.log_message("rejected: proxy secret missing or wrong for op=%s", op)
                self._send(403, "text/plain", b"request did not arrive through the EST proxy")
                return

        client_cert_pem = client_cert_from_header(self.headers.get("X-SSL-Client-Cert"))

        if op == "simplereenroll":
            if client_cert_pem is None:
                self._send(401, "text/plain", b"reenroll requires client cert (X-SSL-Client-Cert missing)")
                return
            if REENROLL_VERIFY_CERT:
                # The BIG-IP's own verdict is advisory only: it is meaningful
                # just when the client-ssl profile has a ca-file, which this
                # project does not configure, so an unverifiable result here is
                # normal rather than an attack. The chain check below is the
                # authoritative one and does not depend on profile settings.
                verify_result = (self.headers.get("X-SSL-Client-Verify") or "").strip()
                if verify_result and verify_result.lower() not in ("ok", "0"):
                    self.log_message("note: proxy reported client cert verify=%s "
                                     "(set a ca-file on the client-ssl profile to make this "
                                     "meaningful); verifying against the issuing CA instead",
                                     verify_result)
                ok, detail = verify_client_cert(client_cert_pem)
                if not ok:
                    self.log_message("reenroll rejected: client cert did not verify (%s)", detail)
                    self._send(403, "text/plain",
                               f"client certificate did not verify against the issuing CA: {detail}".encode())
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
            if auth_throttled(ldap_username):
                self.log_message("throttled: too many failures for '%s'", ldap_username)
                self._send(429, "text/plain",
                           b"too many failed authentication attempts; try again later",
                           {"Retry-After": str(AUTH_WINDOW_SECONDS)})
                return
            ok, detail = ldap_authenticate(ldap_username, ldap_password)
            if not ok:
                record_auth_failure(ldap_username)
                self._send(403, "text/plain", f"LDAP authentication failed: {detail}".encode())
                return
            clear_auth_failures(ldap_username)

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
                # A CN check alone is not a name binding: the role signs with
                # use_csr_sans, TLS peers validate against SANs, so an otherwise
                # valid CSR can name somebody else in a SAN.
                extra = [n for n in csr_san_dns_names(csr_pem) if n != ldap_username]
                if extra:
                    self._send(403, "text/plain",
                               ("CSR requests SAN(s) not belonging to the authenticated user "
                                f"'{ldap_username}': {', '.join(extra)}").encode())
                    return

            if op == "simplereenroll" and REENROLL_VERIFY_CERT and client_cert_pem:
                # RFC 7030 reenrolment replaces a certificate; it does not grant
                # the holder a new name. Bind the request to the identity that
                # authenticated it.
                current = x509_common_name(client_cert_pem)
                requested = csr_common_name(csr_pem)
                if current != requested:
                    self._send(403, "text/plain",
                               (f"reenroll CN '{requested}' does not match the presented "
                                f"certificate '{current}'").encode())
                    return
                extra = [n for n in csr_san_dns_names(csr_pem) if n != current]
                if extra:
                    self._send(403, "text/plain",
                               ("reenroll requests SAN(s) not on the presented certificate: "
                                f"{', '.join(extra)}").encode())
                    return
            try:
                signed = bao_request("POST", f"{PKI_MOUNT}/sign/{PKI_ROLE}", token=token,
                                      body={"csr": csr_pem})
            except urllib.error.HTTPError as e:
                self._send(502, "text/plain", f"OpenBao sign error: {e.read()}".encode())
                return
            except urllib.error.URLError as e:
                self._send(502, "text/plain",
                           f"OpenBao unreachable at {BAO_ADDR}: {e.reason}".encode())
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
            except urllib.error.URLError as e:
                self._send(502, "text/plain",
                           f"OpenBao unreachable at {BAO_ADDR}: {e.reason}".encode())
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
