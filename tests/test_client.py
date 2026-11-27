"""Offline tests for TLS chain repair — no LMS or network needed."""

import io
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from moodle_mcp import client  # noqa: E402


def _fake_server(monkeypatch, tmp_path, good=b"NEW", fetched=b"NEW"):
    """Serve OK only when the trust store is a bundle containing `good`."""
    monkeypatch.setattr(client, "CACHE", tmp_path)
    monkeypatch.setattr(client, "_legacy_context", lambda cafile=None: cafile)
    fetches = []

    def fetch(host, port=443, refresh=False):
        fetches.append(refresh)
        bundle = tmp_path / f"{host}-chain.pem"
        if bundle.exists() and not refresh:
            return bundle
        bundle.write_bytes(fetched)
        return bundle

    class Opener:
        def __init__(self, ctx):
            self.ctx = ctx

        def open(self, req, timeout=None):
            if self.ctx and Path(self.ctx).read_bytes() == good:
                return io.BytesIO(b"ok")
            err = ssl.SSLCertVerificationError("unable to get local issuer certificate")
            raise urllib.error.URLError(err)

    monkeypatch.setattr(client, "_fetch_missing_intermediate", fetch)
    monkeypatch.setattr(urllib.request, "HTTPSHandler", lambda context: context)
    monkeypatch.setattr(urllib.request, "build_opener", Opener)
    return fetches


def test_stale_cached_bundle_is_refetched(monkeypatch, tmp_path):
    fetches = _fake_server(monkeypatch, tmp_path)
    (tmp_path / "lms.example.edu-chain.pem").write_bytes(b"OLD")
    m = client.Moodle(url="https://lms.example.edu", token="t")
    assert m._open(urllib.request.Request(m.url)) == b"ok"
    assert fetches == [False, True]
    assert (tmp_path / "lms.example.edu-chain.pem").read_bytes() == b"NEW"


def test_fresh_bundle_is_not_refetched(monkeypatch, tmp_path):
    fetches = _fake_server(monkeypatch, tmp_path, fetched=b"BAD")
    m = client.Moodle(url="https://lms.example.edu", token="t")
    try:
        m._open(urllib.request.Request(m.url))
    except urllib.error.URLError:
        pass
    else:
        raise AssertionError("verification failure should propagate")
    assert fetches == [False]


def test_legacy_context_allows_unsafe_legacy_renegotiation():
    # lms.nust.edu.pk fails with UNSAFE_LEGACY_RENEGOTIATION_DISABLED without this OpenSSL option
    ctx = client._legacy_context()
    assert ctx.options & client._ALLOW_UNSAFE_LEGACY_RENEGOTIATION
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname  # certificates are still verified
