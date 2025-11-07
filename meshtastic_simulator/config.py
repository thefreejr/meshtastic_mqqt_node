"""
Конфигурация и константы (из firmware/src)
Читает настройки из config/project.yaml
Использует единый ConfigLoader для устранения дублирования
"""

from .utils.logger import LogLevel
from .config_loader import ConfigLoader

# Используем единый загрузчик конфигурации
_config_loader = ConfigLoader.get_instance()

def _get_config_value(path: str, default):
    """Получает значение из конфигурации по пути (например, 'mqtt.address')"""
    return _config_loader.get_value(path, default)

# StreamAPI константы (из firmware/src/mesh/StreamAPI.cpp)
START1 = _get_config_value('constants.streamapi.start1', 0x94)
START2 = _get_config_value('constants.streamapi.start2', 0xC3)
HEADER_LEN = _get_config_value('constants.streamapi.header_len', 4)
MAX_TO_FROM_RADIO_SIZE = _get_config_value('constants.streamapi.max_to_from_radio_size', 512)

# Channels константы (из firmware/src/mesh/Channels.h)
MAX_NUM_CHANNELS = _get_config_value('constants.channels.max_num_channels', 8)

# MQTT константы (из firmware/src/mqtt/MQTT.h)
DEFAULT_MQTT_ADDRESS = _get_config_value('mqtt.address', "mqtt.meshtastic.org")
DEFAULT_MQTT_USERNAME = _get_config_value('mqtt.username', "meshdev")
DEFAULT_MQTT_PASSWORD = _get_config_value('mqtt.password', "large4cats")
DEFAULT_MQTT_ROOT = _get_config_value('mqtt.root', "msh")

# Hop limit константы (из firmware/src/mesh/MeshTypes.h)
HOP_MAX = _get_config_value('constants.hop.max', 7)
DEFAULT_HOP_LIMIT = _get_config_value('constants.hop.default_limit', 3)

# Логирование
_log_level_str = _get_config_value('logging.level', 'DEBUG')
_log_level_map = {
    'DEBUG': LogLevel.DEBUG,
    'INFO': LogLevel.INFO,
    'WARN': LogLevel.WARN,
    'ERROR': LogLevel.ERROR,
    'NONE': LogLevel.NONE
}
DEFAULT_LOG_LEVEL = _log_level_map.get(_log_level_str.upper(), LogLevel.DEBUG)
DEFAULT_LOG_CATEGORIES = _get_config_value('logging.categories', None)
DEFAULT_LOG_FILE = _get_config_value('logging.file', None)

# TCP настройки
TCP_HOST = _get_config_value('tcp.host', '0.0.0.0')
TCP_PORT = _get_config_value('tcp.port', 4403)

# NodeConfig перенесен в meshtastic_simulator.mesh.config_storage для избежания дублирования
# Импортируем для обратной совместимости
from .mesh.config_storage import NodeConfig

