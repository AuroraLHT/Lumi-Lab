"""lumi.tls: when the API and MCP servers serve HTTPS, and what the launchers' preflight
says about the certificate."""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from lumi import tls


def _write_cert(tmp_path, names: list[str], days: float = 365):
    key = ec.generate_private_key(ec.SECP256R1())
    sans = []
    for name in names:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            sans.append(x509.DNSName(name))
    now = datetime.now(timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = tmp_path / "server.crt", tmp_path / "server.key"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return certfile, keyfile


@pytest.fixture
def use_tls(monkeypatch):
    """Point the module's settings lookup at a given [tls] table."""
    def apply(table):
        monkeypatch.setattr(tls, "_table", lambda: dict(table))
    return apply


def test_off_by_default_serves_plain_http(use_tls):
    use_tls({"enabled": False, "certfile": "nope", "keyfile": "nope"})
    assert tls.tls_files() is None
    assert tls.uvicorn_ssl_kwargs() == {}
    assert tls.scheme() == "http"


def test_enabled_hands_uvicorn_the_files(tmp_path, use_tls):
    certfile, keyfile = _write_cert(tmp_path, ["10.0.0.5"])
    use_tls({"enabled": True, "certfile": str(certfile), "keyfile": str(keyfile)})
    assert tls.uvicorn_ssl_kwargs() == {"ssl_certfile": str(certfile), "ssl_keyfile": str(keyfile)}
    assert tls.scheme() == "https"


def test_enabled_with_a_missing_file_refuses_rather_than_falling_back(tmp_path, use_tls):
    certfile, _ = _write_cert(tmp_path, ["10.0.0.5"])
    use_tls({"enabled": True, "certfile": str(certfile), "keyfile": str(tmp_path / "gone.key")})
    with pytest.raises(tls.TLSConfigError, match="keyfile"):
        tls.uvicorn_ssl_kwargs()
    use_tls({"enabled": True, "certfile": str(certfile)})
    with pytest.raises(tls.TLSConfigError, match="not set"):
        tls.tls_files()


def test_relative_paths_are_from_the_project_root(use_tls):
    use_tls({"enabled": True, "certfile": "pyproject.toml", "keyfile": "pyproject.toml"})
    files = tls.tls_files()
    assert files.certfile == tls.PROJECT_ROOT / "pyproject.toml"


def test_cert_covers_exact_ips_and_names(tmp_path):
    certfile, _ = _write_cert(tmp_path, ["10.0.0.5", "lab.example", "*.lab.example"])
    info = tls.cert_info(certfile)
    assert info.covers("10.0.0.5")
    assert not info.covers("10.0.0.6")
    assert info.covers("lab.example")
    assert info.covers("mcp.lab.example")
    assert not info.covers("a.b.lab.example")
    assert not info.covers("other.example")


def test_preflight_reports_coverage_and_expiry(tmp_path, use_tls):
    certfile, keyfile = _write_cert(tmp_path, ["10.0.0.5"], days=10)
    use_tls({"enabled": True, "certfile": str(certfile), "keyfile": str(keyfile)})
    report = tls.preflight("10.0.0.5")
    assert any(lvl == "warn" and "expires in" in msg for lvl, msg in report)  # 10 days left
    assert any(lvl == "ok" and "covers 10.0.0.5" in msg for lvl, msg in report)
    assert any(lvl == "fail" and "does not cover" in msg for lvl, msg in tls.preflight("10.0.0.9"))

    use_tls({"enabled": False})
    assert tls.preflight("10.0.0.5") == [("off", "tls.enabled is false -- serving plain HTTP")]


def test_default_host_prefers_the_certificates_dns_name(tmp_path, use_tls):
    # A Tailscale cert names only the MagicDNS host; clients must use that, not an IP.
    certfile, keyfile = _write_cert(tmp_path, ["localhost", "lab.tail1234.ts.net", "127.0.0.1"])
    use_tls({"enabled": True, "certfile": str(certfile), "keyfile": str(keyfile)})
    assert tls.default_host() == "lab.tail1234.ts.net"

    certfile, keyfile = _write_cert(tmp_path, ["localhost", "127.0.0.1", "10.0.0.5"])
    assert tls.default_host() == "10.0.0.5"

    use_tls({"enabled": False})
    assert tls.default_host() is None


_SERVER = """
import asyncio, sys, uvicorn
from lumi.tls import reloading_server, uvicorn_ssl_kwargs

async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            else:
                await send({"type": "lifespan.shutdown.complete"})
                return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})

config = uvicorn.Config(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning",
                        **uvicorn_ssl_kwargs())
asyncio.run(reloading_server(config))
"""


def _served_cert(port: int) -> bytes:
    import socket
    import ssl

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        with context.wrap_socket(sock) as tls_sock:
            return tls_sock.getpeercert(binary_form=True)


def test_sighup_swaps_in_a_renewed_certificate_without_a_restart(tmp_path):
    import os
    import signal
    import socket
    import subprocess
    import sys
    import time

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir(), second.mkdir()
    old_cert, old_key = _write_cert(first, ["127.0.0.1"])
    new_cert, new_key = _write_cert(second, ["127.0.0.1"])
    live_cert, live_key = tmp_path / "server.crt", tmp_path / "server.key"
    live_cert.write_bytes(old_cert.read_bytes())
    live_key.write_bytes(old_key.read_bytes())

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = dict(os.environ, DYNACONF_TLS__ENABLED="true",
               DYNACONF_TLS__CERTFILE=str(live_cert), DYNACONF_TLS__KEYFILE=str(live_key))
    proc = subprocess.Popen([sys.executable, "-c", _SERVER, str(port)], env=env)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                before = _served_cert(port)
                break
            except OSError:
                assert proc.poll() is None, "server exited"
                assert time.monotonic() < deadline, "server never came up"
                time.sleep(0.1)

        live_key.write_bytes(new_key.read_bytes())
        live_cert.write_bytes(new_cert.read_bytes())
        proc.send_signal(signal.SIGHUP)
        deadline = time.monotonic() + 5
        while (after := _served_cert(port)) == before and time.monotonic() < deadline:
            time.sleep(0.1)

        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        expected = x509.load_pem_x509_certificate(new_cert.read_bytes()).public_bytes(
            serialization.Encoding.DER)
        assert after == expected
        assert proc.poll() is None, "SIGHUP must not kill the server"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
