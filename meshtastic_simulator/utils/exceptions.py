"""
Централизованная обработка ошибок для Meshtastic MQTT Node Simulator
"""


class MeshSimulatorError(Exception):
    """Базовый класс для всех ошибок симулятора"""
    pass


class MeshProtocolError(MeshSimulatorError):
    """Ошибки протокола Meshtastic"""
    def __init__(self, message: str, packet_id: int = None, node_id: str = None):
        super().__init__(message)
        self.packet_id = packet_id
        self.node_id = node_id


class MQTTConnectionError(MeshSimulatorError):
    """Ошибки подключения к MQTT брокеру"""
    def __init__(self, message: str, broker: str = None, port: int = None, reason_code=None):
        super().__init__(message)
        self.broker = broker
        self.port = port
        self.reason_code = reason_code


class MQTTSubscriptionError(MeshSimulatorError):
    """Ошибки подписки на MQTT топики"""
    def __init__(self, message: str, topic: str = None):
        super().__init__(message)
        self.topic = topic


class ConfigError(MeshSimulatorError):
    """Ошибки конфигурации"""
    def __init__(self, message: str, config_path: str = None):
        super().__init__(message)
        self.config_path = config_path


class PacketProcessingError(MeshSimulatorError):
    """Ошибки обработки пакетов"""
    def __init__(self, message: str, packet_id: int = None, packet_type: str = None):
        super().__init__(message)
        self.packet_id = packet_id
        self.packet_type = packet_type


class PersistenceError(MeshSimulatorError):
    """Ошибки сохранения/загрузки данных"""
    def __init__(self, message: str, file_path: str = None):
        super().__init__(message)
        self.file_path = file_path


class CryptoError(MeshSimulatorError):
    """Ошибки криптографии"""
    def __init__(self, message: str, operation: str = None):
        super().__init__(message)
        self.operation = operation


