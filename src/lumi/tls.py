"""HTTPS for the two servers a browser or agent connects to: the API bridge and the
MCP server's HTTP transport.

Both run on uvicorn, which terminates TLS itself given a certificate and key, so there
is no proxy in between. One `[tls]` block configures both:

    [tls]
    enabled = true
    certfile = "cfg/tls/server.crt"
    keyfile = "cfg/tls/server.key"

Where the files come from:

- `scripts/tailscale_cert.sh` (the normal case): a Let's Encrypt certificate for this
  machine's Tailscale name, trusted by every browser and phone with nothing installed
  on the client. `--install-cron` renews it daily and signals the servers to reload.
- `scripts/make_lab_cert.sh`: a lab CA and a certificate for the LAN address, for a
  host with no Tailscale. Every client then has to trust the CA by hand.

The block belongs in cfg/.secrets.toml on the server host; a simulation stack exports
DYNACONF_TLS__ENABLED=false so it stays on plain HTTP over loopback.

A renewed certificate reaches running servers on SIGHUP (`reloading_server`), so a
renewal never needs a restart.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from lumi.config import settings
from lumi.path import PROJECT_ROOT

log = logging.getLogger(__name__)


class TLSConfigError(RuntimeError):
    """`[tls]` is enabled but cannot be served from."""


@dataclass(frozen=True)
class TLSFiles:
    certfile: Path
    keyfile: Path

    def uvicorn_kwargs(self) -> dict[str, str]:
        return {"ssl_certfile": str(self.certfile), "ssl_keyfile": str(self.keyfile)}


def _resolve(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _table() -> dict:
    return dict(settings.get("tls", {}) or {})


def tls_files() -> TLSFiles | None:
    """The certificate and key to serve with, or None when TLS is off.

    Raises TLSConfigError when it is on but a file is unset or missing -- a server
    that silently fell back to plain HTTP would put passwords on the wire."""
    cfg = _table()
    if not bool(cfg.get("enabled", False)):
        return None
    paths = {}
    for key in ("certfile", "keyfile"):
        value = cfg.get(key)
        if not value:
            raise TLSConfigError(f"tls.enabled is true but tls.{key} is not set")
        path = _resolve(str(value))
        if not path.is_file():
            raise TLSConfigError(f"tls.{key} {path} does not exist -- scripts/tailscale_cert.sh (or make_lab_cert.sh)")
        paths[key] = path
    return TLSFiles(**paths)


def uvicorn_ssl_kwargs() -> dict[str, str]:
    files = tls_files()
    return files.uvicorn_kwargs() if files else {}


def scheme() -> str:
    return "https" if tls_files() else "http"


@dataclass(frozen=True)
class CertInfo:
    names: tuple[str, ...]
    not_after: datetime

    @property
    def days_left(self) -> float:
        return (self.not_after - datetime.now(timezone.utc)).total_seconds() / 86400

    def covers(self, host: str) -> bool:
        """Whether a client connecting to `host` would accept this certificate.
        Exact match for IPs; DNS names allow one leading `*.` label."""
        host = host.strip("[]").lower()
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        for name in self.names:
            if ip is not None:
                try:
                    if ipaddress.ip_address(name) == ip:
                        return True
                except ValueError:
                    continue
            elif name.startswith("*."):
                if host.endswith(name[1:]) and host.count(".") == name.count("."):
                    return True
            elif name.lower() == host:
                return True
        return False


def cert_info(certfile: Path) -> CertInfo:
    """Subject alternative names and expiry of a PEM certificate."""
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(Path(certfile).read_bytes())
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(n) for n in san.get_values_for_type(x509.DNSName)]
        names += [str(n) for n in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    return CertInfo(names=tuple(names), not_after=cert.not_valid_after_utc)


def default_host() -> str | None:
    """The name clients should use: the certificate's first fully qualified DNS name
    (a Tailscale cert has only that one), else its first non-loopback IP. None when TLS
    is off or the certificate cannot be read -- the caller picks its own default."""
    try:
        files = tls_files()
        info = cert_info(files.certfile) if files else None
    except Exception:
        return None
    if info is None:
        return None
    for name in info.names:
        if "." in name and not name.startswith("*."):
            try:
                ipaddress.ip_address(name)
            except ValueError:
                return name
    for name in info.names:
        try:
            if not ipaddress.ip_address(name).is_loopback:
                return name
        except ValueError:
            continue
    return None


async def reloading_server(config) -> None:
    """Run a uvicorn Config in this process, reloading its certificate on SIGHUP.

    uvicorn builds one SSLContext at startup and never looks at the files again;
    load_cert_chain on that same context makes every *new* connection present the
    renewed certificate, while open ones (a /ws session) carry on undisturbed. The
    handler is installed with or without TLS: an unhandled SIGHUP would kill the
    process, and the renewal job does not know which servers have TLS on.

    (A multi-worker uvicorn.run needs none of this: its supervisor restarts the
    workers on SIGHUP and each one loads the files afresh.)"""
    import uvicorn

    config.load()  # builds config.ssl; Server.serve() skips it once loaded
    files = tls_files()

    def reload() -> None:
        if config.ssl is None or files is None:
            log.info("SIGHUP: TLS is off, nothing to reload")
            return
        try:
            config.ssl.load_cert_chain(str(files.certfile), str(files.keyfile))
            log.info("SIGHUP: reloaded %s", files.certfile)
        except Exception:
            log.exception("SIGHUP: could not reload %s; still serving the old one", files.certfile)

    asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, reload)
    await uvicorn.Server(config).serve()


def preflight(host: str | None) -> list[tuple[str, str]]:
    """(level, message) lines for a launcher's preflight: level is ok / warn / fail /
    off. `host` is the name or address clients will connect by; None skips that check."""
    try:
        files = tls_files()
    except TLSConfigError as exc:
        return [("fail", str(exc))]
    if files is None:
        return [("off", "tls.enabled is false -- serving plain HTTP")]
    lines = [("ok", f"TLS on ({files.certfile})")]
    try:
        info = cert_info(files.certfile)
    except Exception as exc:  # unreadable / not PEM
        return lines + [("fail", f"cannot read {files.certfile}: {exc}")]
    days = info.days_left
    if days <= 0:
        lines.append(("fail", f"certificate expired {info.not_after:%Y-%m-%d} -- rerun the script that made it"))
    elif days < 30:
        lines.append(("warn", f"certificate expires in {days:.0f} days -- is the renewal cron running? (scripts/tailscale_cert.sh --install-cron)"))
    else:
        lines.append(("ok", f"certificate valid until {info.not_after:%Y-%m-%d}"))
    if host:
        if info.covers(host):
            lines.append(("ok", f"certificate covers {host}"))
        else:
            lines.append(("fail", f"certificate does not cover {host} (it lists {', '.join(info.names)})"
                                  f" -- connect by a name it lists, or reissue it"))
    return lines


if __name__ == "__main__":
    import sys

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "--default-host":
        print(default_host() or "")
    else:
        for level, message in preflight(arg or default_host()):
            print(f"{level}|{message}")
