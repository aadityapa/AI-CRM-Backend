"""
Create self-signed TLS certs for local HTTPS (camera/mic on LAN).
No OpenSSL binary required — uses the `cryptography` package.

SAN includes: localhost, 127.0.0.1, ::1, and your current LAN IPv4 when detectable.
"""
from __future__ import annotations

import datetime
from datetime import timezone
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def _detect_lan_ipv4() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip if ip and ip != "127.0.0.1" else None
    except OSError:
        return None


def main() -> int:
    cert_dir = Path(__file__).resolve().parent / "certs"
    cert_dir.mkdir(parents=True, exist_ok=True)
    key_path = cert_dir / "key.pem"
    cert_path = cert_dir / "cert.pem"

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "karnex-dev-local"),
        ]
    )

    san_list: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv6Address("::1")),
    ]
    lip = _detect_lan_ipv4()
    if lip:
        try:
            san_list.append(x509.IPAddress(ipaddress.IPv4Address(lip)))
            print(f"Including LAN IP in certificate: {lip}")
        except ValueError:
            pass

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(timezone.utc) - datetime.timedelta(minutes=1))
        .not_valid_after(datetime.datetime.now(timezone.utc) + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
    )

    cert = builder.sign(key, hashes.SHA256())

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    print(f"Wrote {key_path}")
    print(f"Wrote {cert_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
