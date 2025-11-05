"""
Конфигурация и константы (из firmware/src)
"""

from .utils.logger import LogLevel

# StreamAPI константы (из firmware/src/mesh/StreamAPI.cpp)
START1 = 0x94
START2 = 0xC3
HEADER_LEN = 4
MAX_TO_FROM_RADIO_SIZE = 512

# Channels константы (из firmware/src/mesh/Channels.h)
MAX_NUM_CHANNELS = 8

# MQTT константы (из firmware/src/mqtt/MQTT.h)
DEFAULT_MQTT_ADDRESS = "mqtt.meshtastic.org"
DEFAULT_MQTT_USERNAME = "meshdev"
DEFAULT_MQTT_PASSWORD = "large4cats"
DEFAULT_MQTT_ROOT = "msh"

# Hop limit константы (из firmware/src/mesh/MeshTypes.h)
HOP_MAX = 7  # Максимальное количество хопов
DEFAULT_HOP_LIMIT = 3  # Дефолтный hop_limit для пакетов (из firmware Default::getConfiguredOrDefaultHopLimit)

# Логирование
# Доступные уровни: DEBUG, INFO, WARN, ERROR, NONE
# DEBUG - все сообщения включая отладочные
# INFO - информационные сообщения и выше (по умолчанию)
# WARN - только предупреждения и ошибки
# ERROR - только ошибки
# NONE - отключить логирование
DEFAULT_LOG_LEVEL = LogLevel.DEBUG

# Фильтрация категорий логов
# None или пустой список = логировать все категории
# Список категорий = логировать только указанные категории
# Доступные категории: TCP, MQTT, ADMIN, NODE, ACK, PKI, CONFIG, CHANNELS
# Пример: ['TCP', 'MQTT', 'ADMIN'] - только логи TCP, MQTT и ADMIN
# Пример: None - логировать все категории
DEFAULT_LOG_CATEGORIES = None #Все категории разрешены по умолчанию
#DEFAULT_LOG_CATEGORIES = ['TCP', 'ADMIN','CONFIG','CHANNELS'] 

# Конфигурация узла (для _send_config)
class NodeConfig:
    """Конфигурация узла для отправки клиенту"""
    
    # Имена узла
    USER_LONG_NAME = "MQTT Node"
    USER_SHORT_NAME = "MQTT"
    
    # Hardware модель (из meshtastic.protobuf.mesh_pb2.HardwareModel)
    # По умолчанию PORTDUINO (симулятор)
    HW_MODEL = "PORTDUINO"  # Будет преобразовано в enum при использовании
    
    # Device Metrics (по умолчанию)
    # 101 = powered (pwd) - устройство питается от сети, не от батареи
    DEVICE_METRICS_BATTERY_LEVEL = 101
    DEVICE_METRICS_VOLTAGE = 5.0  # Вольты
    DEVICE_METRICS_CHANNEL_UTILIZATION = 0.0  # Процент использования канала
    DEVICE_METRICS_AIR_UTIL_TX = 0.0  # Процент использования эфира для TX
    
    # Metadata
    FIRMWARE_VERSION = "2.6.11.60ec05e"
    
    # Fallback node_num (если не удалось определить из node_id)
    FALLBACK_NODE_NUM = 0x12345678

