"""Moodle web-service client that survives real-world university servers.

Two things break stock HTTP clients against Moodle installs in the wild, both
handled here transparently:

1. Incomplete TLS chains. Many campus servers present only the leaf certificate
   and omit the intermediate. Rather than disabling verification, we chase the
   AIA "CA Issuers" URL in the leaf, download the missing intermediate, cache
   it, and retry with a repaired trust store. Verification stays on.

2. Polluted response bodies. Local plugins sometimes echo HTML before the JSON
   payload (NUST prints an HTML comment on every single response), which makes
   standard JSON parsing fail. We slice from the first structural character.
"""

from __future__ import annotations

import json
import os
import re
import base64
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://lms.nust.edu.pk"
SERVICE = "moodle_mobile_app"
UA = "MoodleMobile/4.5.0 (Linux) moodle-mcp"

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "moodle-mcp"


class MoodleError(RuntimeError):
    """A Moodle-side error (bad login, revoked token, refused function)."""


# --------------------------------------------------------------------------
# TLS repair
# --------------------------------------------------------------------------

# OpenSSL's SSL_OP_ALLOW_UNSAFE_LEGACY_RENEGOTIATION; the ssl module does not name it.
# OP_LEGACY_SERVER_CONNECT alone is not enough for lms.nust.edu.pk under OpenSSL 3:
# the handshake still fails with UNSAFE_LEGACY_RENEGOTIATION_DISABLED.
_ALLOW_UNSAFE_LEGACY_RENEGOTIATION = 0x40000


def _allow_legacy_renegotiation(ctx: ssl.SSLContext) -> None:
    if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    ctx.options |= _ALLOW_UNSAFE_LEGACY_RENEGOTIATION


def _legacy_context(cafile: Path | None = None) -> ssl.SSLContext:
    """Context tolerant of old servers but still verifying certificates."""
    ctx = ssl.create_default_context(cafile=str(cafile) if cafile else None)
    # Campus servers are often stuck on TLS 1.2 without secure renegotiation.
    _allow_legacy_renegotiation(ctx)
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except ssl.SSLError:
        pass
    return ctx


def _der_to_pem(der: bytes) -> bytes:
    b64 = base64.encodebytes(der).decode("ascii")
    return f"-----BEGIN CERTIFICATE-----\n{b64}-----END CERTIFICATE-----\n".encode()


def _peek_leaf(host: str, port: int) -> bytes | None:
    """Grab the server's leaf certificate without trusting it.

    We are only reading the certificate to discover where its issuer lives.
    Nothing is trusted on the basis of this handshake; the real verification
    happens afterwards against the system root store.
    """
    ctx = ssl.create_default_context()
    _allow_legacy_renegotiation(ctx)
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except ssl.SSLError:
        pass
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=20) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                return ss.getpeercert(binary_form=True)
    except OSError:
        return None


def _aia_urls(der: bytes) -> list[str]:
    """Pull CA-Issuers URLs out of a DER certificate.

    The AIA extension stores URLs as plain IA5Strings, so they appear verbatim
    in the DER. Scanning for them avoids a hard dependency on a full ASN.1
    parser just to repair someone else's server config.
    """
    found = re.findall(rb"https?://[\x21-\x7e]{6,200}?\.(?:crt|cer|p7c|p7b)", der)
    return [u.decode("ascii", "ignore") for u in found]


def _fetch_missing_intermediate(host: str, port: int = 443,
                                refresh: bool = False) -> Path | None:
    """Recover the intermediate the server failed to send, and cache a bundle.

    Returns a PEM file of system roots plus the recovered intermediate, or None
    if recovery was not possible (in which case we fail closed, never insecure).
    With refresh=True a cached bundle is discarded and rebuilt.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    bundle = CACHE / f"{host}-chain.pem"
    if bundle.exists():
        if not refresh:
            return bundle
        bundle.unlink()

    der = _peek_leaf(host, port)
    if not der:
        return None

    roots = None
    for cand in ("/etc/ssl/certs/ca-certificates.crt",
                 "/etc/pki/tls/certs/ca-bundle.crt",
                 "/etc/ssl/cert.pem"):
        if Path(cand).exists():
            roots = Path(cand).read_bytes()
            break
    if roots is None:
        try:
            import certifi
            roots = Path(certifi.where()).read_bytes()
        except Exception:
            return None

    extra = b""
    for url in _aia_urls(der)[:3]:
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                blob = r.read()
        except Exception:
            continue
        # Issuer certs are served as DER far more often than PEM.
        extra += blob if b"-----BEGIN CERT" in blob else _der_to_pem(blob)
    if not extra:
        return None

    bundle.write_bytes(roots + b"\n" + extra)
    return bundle


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class Moodle:
    def __init__(self, url: str | None = None, token: str | None = None,
                 timeout: int = 60) -> None:
        self.url = (url or os.environ.get("MOODLE_URL") or DEFAULT_URL).rstrip("/")
        self.host = urllib.parse.urlparse(self.url).hostname or ""
        self.token = token or os.environ.get("MOODLE_TOKEN") or ""
        self.timeout = timeout
        self._ctx = _legacy_context()
        self._repairs = 0
        self._siteinfo: dict | None = None

    # -- transport ---------------------------------------------------------

    def _open(self, req: urllib.request.Request) -> bytes:
        try:
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=self._ctx))
            with opener.open(req, timeout=self.timeout) as r:
                return r.read()
        except urllib.error.URLError as e:
            broken_chain = isinstance(e.reason, ssl.SSLCertVerificationError)
            if broken_chain and self._repairs < 2:
                # Server omitted its intermediate. Repair trust, then retry. If
                # a cached bundle still fails, the cert was renewed and the
                # intermediate changed: re-fetch it and retry once more.
                cached = (CACHE / f"{self.host}-chain.pem").exists()
                refresh = self._repairs == 1
                self._repairs = 1 if cached and not refresh else 2
                bundle = _fetch_missing_intermediate(self.host, refresh=refresh)
                if bundle:
                    self._ctx = _legacy_context(bundle)
                    return self._open(req)
                raise MoodleError(
                    f"TLS verification failed for {self.host} and the missing "
                    f"intermediate could not be recovered: {e.reason}") from e
            raise

    @staticmethod
    def _parse(raw: bytes) -> Any:
        """Strip any preamble a local plugin printed before the JSON body."""
        text = raw.decode("utf8", "replace")
        spots = [p for p in (text.find("{"), text.find("[")) if p != -1]
        if not spots:
            raise MoodleError(f"non-JSON response: {text[:200]}")
        return json.loads(text[min(spots):])

    # -- auth --------------------------------------------------------------

    def login(self, username: str, password: str, save: bool = True) -> str:
        data = urllib.parse.urlencode(
            {"username": username, "password": password, "service": SERVICE}
        ).encode()
        req = urllib.request.Request(f"{self.url}/login/token.php", data=data,
                                     headers={"User-Agent": UA})
        out = self._parse(self._open(req))
        if "token" not in out:
            raise MoodleError(out.get("error") or f"login failed: {out}")
        self.token = out["token"]
        if save:
            self._save_token()
        return self.token

    def _token_path(self) -> Path:
        return CACHE / f"{self.host}.token.json"

    def _save_token(self) -> None:
        CACHE.mkdir(parents=True, exist_ok=True)
        p = self._token_path()
        p.write_text(json.dumps({"url": self.url, "token": self.token}))
        p.chmod(0o600)

    def load_cached_token(self) -> bool:
        p = self._token_path()
        if p.exists():
            self.token = json.loads(p.read_text()).get("token", "")
            return bool(self.token)
        return False

    def ensure_auth(self) -> None:
        """Token from env, else cache, else MOODLE_USER/MOODLE_PASS."""
        if self.token:
            return
        if self.load_cached_token():
            return
        user = os.environ.get("MOODLE_USER")
        pw = os.environ.get("MOODLE_PASS")
        if user and pw:
            self.login(user, pw)
            return
        raise MoodleError(
            "No credentials. Set MOODLE_TOKEN, or MOODLE_USER + MOODLE_PASS, "
            "or run `moodle-mcp login`.")

    # -- web services ------------------------------------------------------

    def call(self, fn: str, **params: Any) -> Any:
        """Invoke any Moodle web-service function.

        Lists/dicts are flattened into Moodle's PHP-style bracket syntax, so
        call(..., courseids=[1, 2]) becomes courseids[0]=1&courseids[1]=2.
        """
        self.ensure_auth()
        flat: dict[str, Any] = {
            "wstoken": self.token,
            "wsfunction": fn,
            "moodlewsrestformat": "json",
        }

        def walk(key: str, val: Any) -> None:
            if isinstance(val, (list, tuple)):
                for i, v in enumerate(val):
                    walk(f"{key}[{i}]", v)
            elif isinstance(val, dict):
                for k, v in val.items():
                    walk(f"{key}[{k}]", v)
            elif isinstance(val, bool):
                flat[key] = int(val)
            elif val is not None:
                flat[key] = val

        for k, v in params.items():
            walk(k, v)

        req = urllib.request.Request(
            f"{self.url}/webservice/rest/server.php",
            data=urllib.parse.urlencode(flat).encode(),
            headers={"User-Agent": UA},
        )
        out = self._parse(self._open(req))
        if isinstance(out, dict):
            if out.get("errorcode") in ("invalidtoken", "accessexception"):
                raise MoodleError(
                    f"{out.get('errorcode')}: {out.get('message')} "
                    "(token may have been revoked; re-run login)")
            if "exception" in out:
                raise MoodleError(f"{out.get('errorcode')}: {out.get('message')}")
        return out

    # -- convenience -------------------------------------------------------

    @property
    def siteinfo(self) -> dict:
        if self._siteinfo is None:
            self._siteinfo = self.call("core_webservice_get_site_info")
        return self._siteinfo

    @property
    def userid(self) -> int:
        return int(self.siteinfo["userid"])

    def has(self, fn: str) -> bool:
        """Is a web-service function exposed to this token?"""
        return any(f["name"] == fn for f in self.siteinfo.get("functions", []))

    def download(self, fileurl: str, dest: Path) -> Path:
        """Download a pluginfile URL, authenticating via token."""
        self.ensure_auth()
        sep = "&" if "?" in fileurl else "?"
        req = urllib.request.Request(f"{fileurl}{sep}token={self.token}",
                                     headers={"User-Agent": UA})
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self._open(req))
        return dest
