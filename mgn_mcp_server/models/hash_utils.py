import hashlib


def sha256_hex(value):
    return hashlib.sha256(value.encode()).hexdigest()
