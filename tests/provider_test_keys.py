from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa


def apple_private_key_pair() -> tuple[ec.EllipticCurvePrivateKey, str]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return private_key, private_key_pem


def apple_private_key_pem() -> str:
    return apple_private_key_pair()[1]


def oidc_rsa_private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)
