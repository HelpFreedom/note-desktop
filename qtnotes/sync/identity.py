"""Личность устройства: ключ + самоподписанный сертификат + стабильный device_id.

device_id = первые 16 hex-символов sha256 от DER-сертификата (как у Syncthing —
идентификатор выводится из ключа, подделать нельзя). Ключ и cert лежат в
config.device_dir() (per-installation, не в vault, не синхронизируются).

Сертификат используется для взаимного TLS: пиры закрепляют (pin) cert друг друга
при сопряжении и сверяют его при каждом соединении.
"""

from __future__ import annotations

import datetime
import hashlib
import socket
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .. import config
from ..crypto import primitives as P

_KEY_NAME = "device_key.pem"
_CERT_NAME = "device_cert.pem"

# Маркер зашифрованного-at-rest приватного ключа устройства (P10). Без маркера файл —
# legacy-plaintext (обратная совместимость). Ключ шифрования выводится из НЕИЗВЛЕКАЕМОГО
# TPM-ключа устройства → украденный образ диска не даёт приватный TLS-ключ (нет
# импpersonation'а). TLS грузит ключ из файла, поэтому в рантайме расшифровываем во
# временный tmpfs-файл (RAM), как индекс/блобы.
_KEY_MAGIC = b"QTNK1\n"
_KEY_INFO = b"qtnotes/sync-identity-key/v1"


def _tpm_available() -> bool:
    try:
        from ..crypto import tpm
        return tpm.available()
    except Exception:  # noqa: BLE001
        return False


def _data_key(d: Path) -> bytes:
    """32-байтный ключ шифрования приватного ключа, выведенный из TPM-ключа (под каталогом
    устройства d). Создаёт TPM hmac-ключ в d/tpm/, если его нет."""
    from ..crypto import tpm
    return P.hkdf(tpm.hmac(d, _KEY_INFO), info=_KEY_INFO)


def _runtime_key_path(d: Path) -> Path:
    """Путь к РАСШИФРОВАННОМУ ключу в tmpfs (RAM) — для load_cert_chain. Уникален на
    каталог устройства, чтобы независимые личности (тесты) не пересекались."""
    h = hashlib.sha256(str(d.resolve()).encode("utf-8")).hexdigest()[:16]
    return config.tmpfs_dir(f"qtnotes-syncid-{h}") / _KEY_NAME


def _materialize_runtime(d: Path, key_pem: bytes) -> Path:
    """Положить plaintext-ключ в tmpfs (0600) и вернуть путь."""
    rp = _runtime_key_path(d)
    rp.write_bytes(key_pem)
    try:
        rp.chmod(0o600)
    except OSError:
        pass
    return rp


def _try_encrypt_at_rest(d: Path, disk_path: Path, key_pem: bytes) -> Path | None:
    """Зашифровать ключ на диске под TPM с ПРОВЕРКОЙ расшифровки перед заменой plaintext.
    Возвращает tmpfs-путь к ключу или None (TPM недоступен/сбой → не трогаем plaintext)."""
    try:
        key = _data_key(d)
        enc = _KEY_MAGIC + P.seal(key, key_pem)
        if P.open_sealed(key, enc[len(_KEY_MAGIC):]) != key_pem:
            return None  # расшифровка не сошлась — НЕ заменяем рабочий plaintext
        from .. import fsutil
        fsutil.atomic_write_bytes(disk_path, enc)
        try:
            disk_path.chmod(0o600)
        except OSError:
            pass
        return _materialize_runtime(d, key_pem)
    except Exception:  # noqa: BLE001 — любая заминка TPM: остаёмся на plaintext
        return None


@dataclass(frozen=True)
class Identity:
    device_id: str       # 16 hex — стабильный идентификатор устройства
    fingerprint: str     # полный sha256(DER cert) hex — для сверки/QR
    name: str            # человекочитаемое имя (hostname)
    cert_pem: bytes
    key_pem: bytes
    cert_path: Path
    key_path: Path


def device_name() -> str:
    try:
        return socket.gethostname() or "QtNotes"
    except OSError:
        return "QtNotes"


def fingerprint_from_cert_pem(cert_pem: bytes) -> str:
    """Полный sha256(DER) сертификата в hex (для сверки и QR)."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()


def device_id_from_cert_pem(cert_pem: bytes) -> str:
    """device_id пира из его сертификата (первые 16 hex от fingerprint)."""
    return fingerprint_from_cert_pem(cert_pem)[:16]


def _generate(name: str) -> tuple[bytes, bytes]:
    """Сгенерировать EC-ключ (P-256) и долгоживущий самоподписанный cert."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    far = datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(far)
        # самоподписанный CA: чтобы мобильный BoringSSL принимал наш cert как
        # доверенный якорь при валидации клиентского сертификата
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, key_agreement=False,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH,
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return key_pem, cert_pem


def _post_generate(d: Path, disk_path: Path, key_pem: bytes) -> Path:
    """Сохранить сгенерированный ключ: под TPM — зашифрованным + tmpfs-рантайм; без TPM —
    plaintext на диск (как раньше). Возвращает путь к ключу для TLS (load_cert_chain)."""
    if _tpm_available():
        enc = _try_encrypt_at_rest(d, disk_path, key_pem)
        if enc is not None:
            return enc
    disk_path.write_bytes(key_pem)  # fallback: без TPM — plaintext (как было)
    try:
        disk_path.chmod(0o600)
    except OSError:
        pass
    return disk_path


def _regenerate(d: Path, name: str) -> tuple[bytes, bytes]:
    """Перевыпустить личность (ключ невосстановим — например, после tpm2_clear).
    Перезаписывает cert на диске; ключ сохраняет вызывающий через _post_generate."""
    key_pem, cert_pem = _generate(name)
    (d / _CERT_NAME).write_bytes(cert_pem)
    return key_pem, cert_pem


def ensure_identity() -> Identity:
    """Загрузить личность устройства или создать при первом обращении."""
    return load_or_create(config.device_dir(), device_name())


def load_or_create(d: Path, name: str) -> Identity:
    """Загрузить/создать личность в каталоге d (для тестов — несколько независимых)."""
    d.mkdir(parents=True, exist_ok=True)
    key_path = d / _KEY_NAME
    cert_path = d / _CERT_NAME

    if key_path.exists() and cert_path.exists():
        raw = key_path.read_bytes()
        cert_pem = cert_path.read_bytes()
        if raw[: len(_KEY_MAGIC)] == _KEY_MAGIC:
            # зашифрован-at-rest → расшифровать ключ в tmpfs для TLS
            try:
                key_pem = P.open_sealed(_data_key(d), raw[len(_KEY_MAGIC):])
                key_path = _materialize_runtime(d, key_pem)
            except Exception:  # noqa: BLE001 — TPM очищен/недоступен: ключ невосстановим
                # как и MK при tpm2_clear — перевыпускаем личность (потребует пересопряжения)
                key_pem, cert_pem = _regenerate(d, name)
                key_path = _post_generate(d, key_path, key_pem)
        else:
            # legacy-plaintext: используем как есть; при наличии TPM мигрируем (с проверкой)
            key_pem = raw
            if _tpm_available():
                migrated = _try_encrypt_at_rest(d, key_path, key_pem)
                if migrated is not None:
                    key_path = migrated
    else:
        key_pem, cert_pem = _generate(name)
        cert_path.write_bytes(cert_pem)
        key_path = _post_generate(d, key_path, key_pem)

    fp = fingerprint_from_cert_pem(cert_pem)
    return Identity(
        device_id=fp[:16],
        fingerprint=fp,
        name=name,
        cert_pem=cert_pem,
        key_pem=key_pem,
        cert_path=cert_path,
        key_path=key_path,
    )
