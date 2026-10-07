"""Собственный TLS-сертификат для домена приложения (вместо автоматического Let's Encrypt).

Клиент загружает цепочку PEM и закрытый ключ без пароля; платформа проверяет их, кладёт в Kubernetes Secret типа kubernetes.io/tls
(только в кластере, в базе платформы и в журнале ключа нет) и привязывает к Ingress приложения. Чужие Secret с таким именем не перезаписываются."""
import base64
import re
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from app.services import kubeclient
from app.services.connections import describe_cert
from app.services.secrets import MANAGED_BY, MANAGED_LABEL, is_managed

MAX_CERT_BYTES, MAX_KEY_BYTES, MAX_CHAIN = 65536, 16384, 6
PEM_CERT = re.compile(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.S)
EC_CURVES = ("secp256r1", "secp384r1", "secp521r1")


class CertError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def secret_name(env) -> str:
    return f"{env.deployment_name}-tls"[:253]


def validate(cert_pem: str, key_pem: str, host: str) -> tuple[dict, str, str]:
    """Проверяет пару «цепочка + ключ» для домена host. Возвращает (сведения, цепочка PEM, ключ PEM PKCS#8)."""
    if len(cert_pem.encode()) > MAX_CERT_BYTES or len(key_pem.encode()) > MAX_KEY_BYTES:
        raise CertError("too_large", "certificate or key is too large")
    blocks = PEM_CERT.findall(cert_pem)
    if not 1 <= len(blocks) <= MAX_CHAIN:
        raise CertError("cert_invalid", f"certificate chain must contain 1..{MAX_CHAIN} PEM certificates")
    try:
        chain = [x509.load_pem_x509_certificate(b.encode()) for b in blocks]
    except Exception:
        raise CertError("cert_invalid", "certificate is not a valid PEM")
    leaf = chain[0]
    try:
        if leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise CertError("leaf_is_ca", "the first certificate must be the server certificate, not a CA (put the server certificate first, then intermediates)")
    except x509.ExtensionNotFound:
        pass
    if "ENCRYPTED" in key_pem:
        raise CertError("key_encrypted", "the private key is protected by a password: remove the password first (openssl pkey -in key.pem -out key-nopass.pem)")
    try:
        key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    except Exception:
        raise CertError("key_invalid", "private key is not a valid unencrypted PEM key")
    if isinstance(key, rsa.RSAPrivateKey):
        if key.key_size < 2048:
            raise CertError("key_weak", "RSA keys shorter than 2048 bits are not accepted")
    elif isinstance(key, ec.EllipticCurvePrivateKey):
        if key.curve.name not in EC_CURVES:
            raise CertError("key_type_unsupported", f"EC curve must be one of {EC_CURVES}")
    else:
        raise CertError("key_type_unsupported", "supported keys: RSA (2048+ bits) and ECDSA (P-256, P-384, P-521)")
    pub = lambda k: k.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if pub(key.public_key()) != pub(leaf.public_key()):
        raise CertError("key_mismatch", "the private key does not match the certificate")
    info = describe_cert(leaf.public_bytes(serialization.Encoding.DER), host)
    if info["expired"]:
        raise CertError("cert_expired", f"the certificate expired on {info['not_after'][:10]}")
    if info["not_yet_valid"]:
        raise CertError("cert_not_yet_valid", f"the certificate is valid only from {info['not_before'][:10]}")
    if not info["host_match"]:
        raise CertError("cert_hostname_mismatch", f"the certificate does not cover {host}: it is issued to {', '.join(info['dns_names'] + info['ips']) or info['subject']}")
    warnings = []
    if info["days_left"] < 30:
        warnings.append("cert_expires_soon")
    if len(chain) == 1 and not info["self_signed"]:
        warnings.append("chain_incomplete")                      # без промежуточных сертификатов часть клиентов не построит цепочку
    for a, b in zip(chain, chain[1:]):
        if a.issuer != b.subject:
            warnings.append("chain_order")
            break
    if info["self_signed"]:
        warnings.append("self_signed")
    info["warnings"] = warnings or None
    chain_pem = "".join(c.public_bytes(serialization.Encoding.PEM).decode() for c in chain)
    key_out = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return info, chain_pem, key_out


def _core(cluster: Optional[str]):
    return kubeclient.core_v1_api(cluster) if cluster else kubeclient.core_v1_api()


def _managed(secret) -> bool:
    return is_managed((secret.metadata.labels or {}) if secret.metadata else {})


def apply_secret(namespace: str, name: str, chain_pem: str, key_pem: str, cluster: Optional[str] = None) -> str:
    """Создаёт или обновляет Secret с сертификатом. Возвращает created | updated. Чужой Secret с таким именем не трогает (CertError secret_not_managed)."""
    body = client.V1Secret(metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels={MANAGED_LABEL: MANAGED_BY}), type="kubernetes.io/tls",
                           data={"tls.crt": base64.b64encode(chain_pem.encode()).decode(), "tls.key": base64.b64encode(key_pem.encode()).decode()})
    core = _core(cluster)
    try:
        core.create_namespaced_secret(namespace, body)
        return "created"
    except ApiException as e:
        if e.status != 409:
            raise
    existing = core.read_namespaced_secret(name, namespace)
    if not _managed(existing):
        raise CertError("secret_not_managed", f"a secret named {name} already exists and was not created by the platform: it is not overwritten")
    core.replace_namespaced_secret(name, namespace, body)
    return "updated"


def read_info(namespace: str, name: str, host: Optional[str], cluster: Optional[str] = None) -> Optional[dict]:
    """Сведения о сертификате из Secret (ключ не читается и не возвращается). None — Secret нет."""
    try:
        sec = _core(cluster).read_namespaced_secret(name, namespace)
    except ApiException as e:
        if e.status == 404:
            return None
        raise
    raw = (sec.data or {}).get("tls.crt")
    if not raw:
        return None
    blocks = PEM_CERT.findall(base64.b64decode(raw).decode("utf-8", "replace"))
    if not blocks:
        return None
    leaf = x509.load_pem_x509_certificate(blocks[0].encode())
    info = describe_cert(leaf.public_bytes(serialization.Encoding.DER), host or "")
    info["managed"] = _managed(sec)
    info["chain_length"] = len(blocks)
    return info


def delete_secret(namespace: str, name: str, cluster: Optional[str] = None) -> bool:
    core = _core(cluster)
    try:
        existing = core.read_namespaced_secret(name, namespace)
    except ApiException as e:
        if e.status == 404:
            return False
        raise
    if not _managed(existing):
        return False
    core.delete_namespaced_secret(name, namespace)
    return True
