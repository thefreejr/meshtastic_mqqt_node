"""
Управление PKI ключами (Curve25519) для Meshtastic
"""

from typing import Optional, Tuple
from ..utils.logger import debug, error
from ..utils.exceptions import CryptoError

try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False
    X25519PrivateKey = None


class PKIManager:
    """Управление PKI ключами для сессии"""
    
    @staticmethod
    def generate_keypair() -> Tuple[Optional[bytes], Optional[bytes]]:
        """
        Генерирует пару PKI ключей (Curve25519)
        
        Returns:
            Tuple[private_key, public_key] или (None, None) если криптография недоступна
        """
        if not CRYPTOGRAPHY_AVAILABLE:
            error("PKI", "Криптография недоступна, PKI ключи не будут сгенерированы")
            return None, None
        
        try:
            private_key = X25519PrivateKey.generate()
            public_key = private_key.public_key()
            
            # Получаем сырые байты
            private_bytes = private_key.private_bytes_raw()
            public_bytes = public_key.public_bytes_raw()
            
            if len(public_bytes) != 32:
                raise CryptoError(f"Неверный размер публичного ключа: {len(public_bytes)} байт (ожидается 32)")
            
            debug("PKI", f"PKI ключи сгенерированы (public_key: {public_bytes.hex()[:16]}...)")
            return private_bytes, public_bytes
            
        except Exception as e:
            error("PKI", f"Ошибка генерации PKI ключей: {e}")
            raise CryptoError(f"Ошибка генерации PKI ключей: {e}", operation="generate_keypair")
    
    @staticmethod
    def is_available() -> bool:
        """Проверяет, доступна ли криптография"""
        return CRYPTOGRAPHY_AVAILABLE


