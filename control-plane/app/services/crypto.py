from cryptography.fernet import Fernet, MultiFernet

from app.config import settings


def get_fernet() -> MultiFernet:
    keys = [k.strip() for k in settings.secret_encryption_key.split(",") if k.strip()]
    if not keys:
        raise RuntimeError("secret_encryption_key is not configured")
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt_value(plaintext: str) -> bytes:
    return get_fernet().encrypt(plaintext.encode())


def decrypt_value(ciphertext: bytes) -> str:
    return get_fernet().decrypt(ciphertext).decode()


def rotate_value(ciphertext: bytes) -> bytes:
    """Перешифровывает значение первым (актуальным) ключом; старые ключи нужны только для чтения."""
    return get_fernet().rotate(ciphertext)
