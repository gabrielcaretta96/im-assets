"""Criptografia da fila: caixa selada X25519 + Fernet.

Quem agenda só precisa da chave PÚBLICA (engine/chave_publica.txt) para criptografar.
Só o robô do GitHub tem a chave PRIVADA (segredo AGENDADOR_CHAVE) para abrir.
"""
import base64
import os

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_RAW = dict(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
_INFO = b"intent-agendador-v1"


def _fernet(shared: bytes, eph_pub: bytes) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_pub, info=_INFO).derive(shared)
    return Fernet(base64.urlsafe_b64encode(key))


def gerar_par():
    priv = X25519PrivateKey.generate()
    priv_b64 = base64.b64encode(priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode()
    pub_b64 = base64.b64encode(priv.public_key().public_bytes(**_RAW)).decode()
    return priv_b64, pub_b64


def selar(dados: bytes, pub_b64: str) -> bytes:
    pub = X25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(**_RAW)
    return eph_pub + _fernet(eph.exchange(pub), eph_pub).encrypt(dados)


def abrir(selado: bytes, priv_b64: str) -> bytes:
    priv = X25519PrivateKey.from_private_bytes(base64.b64decode(priv_b64))
    eph_pub, token = selado[:32], selado[32:]
    shared = priv.exchange(X25519PublicKey.from_public_bytes(eph_pub))
    return _fernet(shared, eph_pub).decrypt(token)


def chave_publica(raiz: str) -> str:
    with open(os.path.join(raiz, "engine", "chave_publica.txt")) as f:
        return f.read().strip()
