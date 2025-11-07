"""
Единый загрузчик конфигурации проекта
Устраняет дублирование загрузки project.yaml
"""

import yaml
from pathlib import Path
from typing import Any, Optional, Dict


class ConfigLoader:
    """
    Загрузчик конфигурации проекта с кэшированием
    
    Загружает конфигурацию из config/project.yaml один раз
    и кэширует результат для последующих обращений.
    """
    
    _instance: Optional['ConfigLoader'] = None
    _config: Optional[Dict[str, Any]] = None
    _config_path: Path = Path(__file__).parent.parent.parent / "config" / "project.yaml"
    
    def __new__(cls):
        """Singleton паттерн - один экземпляр на все приложение"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        """Инициализация загрузчика (вызывается только один раз)"""
        if self._config is None:
            self._load_project_config()
    
    def _load_project_config(self) -> Dict[str, Any]:
        """
        Загружает конфигурацию проекта из YAML файла
        
        Returns:
            Словарь с конфигурацией проекта
        """
        if self._config is not None:
            return self._config
        
        self._config = {}
        try:
            if self._config_path.exists():
                with open(self._config_path, 'r', encoding='utf-8') as f:
                    self._config = yaml.safe_load(f) or {}
            else:
                # Если файл не найден, используем значения по умолчанию
                self._config = self._get_default_config()
        except Exception as e:
            print(f"Ошибка загрузки конфигурации проекта: {e}, используем значения по умолчанию")
            self._config = self._get_default_config()
        
        return self._config
    
    def _get_default_config(self) -> Dict[str, Any]:
        """
        Возвращает конфигурацию по умолчанию
        
        Returns:
            Словарь с дефолтными значениями
        """
        return {
            'tcp': {'host': '0.0.0.0', 'port': 4403},
            'mqtt': {
                'address': 'mqtt.meshtastic.org',
                'port': 1883,
                'username': 'meshdev',
                'password': 'large4cats',
                'root': 'msh'
            },
            'logging': {
                'level': 'DEBUG',
                'categories': None,
                'file': None
            },
            'constants': {
                'streamapi': {'start1': 0x94, 'start2': 0xC3, 'header_len': 4, 'max_to_from_radio_size': 512},
                'channels': {'max_num_channels': 8},
                'hop': {'max': 7, 'default_limit': 3}
            },
            'node': {
                'user_long_name': 'MQTT Node',
                'user_short_name': 'MQTT',
                'hw_model': 'PORTDUINO',
                'device_metrics': {'battery_level': 101, 'voltage': 5.0, 'channel_utilization': 0.0, 'air_util_tx': 0.0},
                'firmware_version': '2.6.11.60ec05e',
                'fallback_node_num': 0x12345678
            }
        }
    
    def get_value(self, path: str, default: Any = None) -> Any:
        """
        Получает значение из конфигурации по пути
        
        Args:
            path: Путь к значению в формате 'mqtt.address' или 'node.user_long_name'
            default: Значение по умолчанию, если путь не найден
            
        Returns:
            Значение из конфигурации или default
        """
        config = self._load_project_config()
        keys = path.split('.')
        value = config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value if value is not None else default
    
    def get_config(self) -> Dict[str, Any]:
        """
        Возвращает полную конфигурацию проекта
        
        Returns:
            Словарь с конфигурацией
        """
        return self._load_project_config()
    
    def reload(self) -> Dict[str, Any]:
        """
        Перезагружает конфигурацию из файла (сбрасывает кэш)
        
        Returns:
            Обновленная конфигурация
        """
        self._config = None
        return self._load_project_config()
    
    @classmethod
    def get_instance(cls) -> 'ConfigLoader':
        """
        Возвращает экземпляр ConfigLoader (singleton)
        
        Returns:
            Экземпляр ConfigLoader
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

