"""
Криптография (PKI ключи)
"""

try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False


class CryptoEngine:
    """Криптографический движок для PKI"""
    
    def __init__(self):
        self.pki_private_key = None
        self.pki_public_key = None
        self._generate_pki_keys()
    
    def _generate_pki_keys(self):
        """Генерирует Curve25519 ключи для PKI"""
        if not CRYPTOGRAPHY_AVAILABLE:
            self.pki_private_key = bytes(32)
            self.pki_public_key = bytes(32)
            return
        
        try:
            private_key_obj = X25519PrivateKey.generate()
            self.pki_private_key = private_key_obj.private_bytes_raw()
            public_key_obj = private_key_obj.public_key()
            self.pki_public_key = public_key_obj.public_bytes_raw()
            print(f"🔑 PKI ключи сгенерированы (public_key: {self.pki_public_key[:8].hex()}...)")
        except Exception as e:
            print(f"⚠ Ошибка генерации PKI ключей: {e}")
            self.pki_private_key = bytes(32)
            self.pki_public_key = bytes(32)

