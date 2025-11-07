"""
Загрузка настроек для сессии
"""

from typing import Optional, Tuple, Any
from ..config import MAX_NUM_CHANNELS
from ..mesh.persistence import Persistence
from ..mesh.channels import Channels
from ..mesh.config_storage import ConfigStorage
from ..utils.logger import info, debug, warn, error
from ..utils.exceptions import PersistenceError

try:
    from meshtastic import mesh_pb2
except ImportError:
    print("Ошибка: Установите meshtastic: pip install meshtastic")
    raise


class SettingsLoader:
    """Загрузчик настроек для сессии"""
    
    def __init__(self, persistence: Persistence, channels: Channels, 
                 config_storage: ConfigStorage, owner: mesh_pb2.User,
                 pki_public_key: Optional[bytes], node_id: str) -> None:
        """
        Инициализирует загрузчик настроек
        
        Args:
            persistence: Объект Persistence для загрузки данных
            channels: Объект Channels для обновления каналов
            config_storage: Объект ConfigStorage для обновления конфигурации
            owner: Объект User (владелец) для обновления
            pki_public_key: Публичный ключ PKI (32 байта) или None
            node_id: Node ID для логирования
        """
        self.persistence = persistence
        self.channels = channels
        self.config_storage = config_storage
        self.owner = owner
        self.pki_public_key = pki_public_key
        self.node_id = node_id
        self.log_prefix = f"[{node_id}]"
    
    def load_all(self) -> int:
        """
        Загружает все сохраненные настройки
        
        Returns:
            Количество загруженных компонентов
        """
        try:
            info("PERSISTENCE", f"{self.log_prefix} Загрузка сохраненных настроек...")
            loaded_count = 0
            
            # Загружаем каналы
            if self._load_channels():
                loaded_count += 1
            
            # Загружаем Config
            if self._load_config():
                loaded_count += 1
            
            # Загружаем ModuleConfig
            if self._load_module_config():
                loaded_count += 1
            
            # Загружаем Owner
            if self._load_owner():
                loaded_count += 1
            
            # Загружаем шаблонные сообщения
            if self._load_canned_messages():
                loaded_count += 1
            
            if loaded_count == 0:
                debug("PERSISTENCE", f"{self.log_prefix} Используются настройки по умолчанию (файл настроек не найден или пуст)")
            else:
                info("PERSISTENCE", f"{self.log_prefix} Загрузка настроек завершена: загружено {loaded_count} компонентов")
            
            return loaded_count
            
        except Exception as e:
            error("PERSISTENCE", f"{self.log_prefix} Ошибка загрузки настроек: {e}")
            raise PersistenceError(f"Ошибка загрузки настроек: {e}", file_path=str(self.persistence.settings_path))
    
    def _load_channels(self) -> bool:
        """Загружает каналы из файла"""
        try:
            saved_channels = self.persistence.load_channels()
            if saved_channels and len(saved_channels) == MAX_NUM_CHANNELS:
                # Проверяем корректность каналов
                for i, ch in enumerate(saved_channels):
                    if ch.index != i:
                        warn("PERSISTENCE", f"{self.log_prefix} Неверный индекс канала {i}: {ch.index}, пропускаем загрузку")
                        return False
                
                self.channels.channels = saved_channels
                self.channels.hashes = {}  # Пересчитываем hashes
                
                info("PERSISTENCE", f"{self.log_prefix} Загружено {len(saved_channels)} каналов из файла (индивидуальные настройки)")
                
                # Логируем статус Custom канала
                if len(saved_channels) > 1:
                    custom_ch = saved_channels[1]
                    info("PERSISTENCE", f"{self.log_prefix} Загружено {len(saved_channels)} каналов. Custom канал (index=1): downlink_enabled={custom_ch.settings.downlink_enabled}, uplink_enabled={custom_ch.settings.uplink_enabled}")
                
                return True
            else:
                if saved_channels:
                    warn("PERSISTENCE", f"{self.log_prefix} Неверное количество каналов: {len(saved_channels)}, ожидалось {MAX_NUM_CHANNELS}")
                else:
                    debug("PERSISTENCE", f"{self.log_prefix} Индивидуальные настройки каналов не найдены, используются дефолтные из node_defaults.json")
                return False
        except Exception as e:
            error("PERSISTENCE", f"{self.log_prefix} Ошибка загрузки каналов: {e}")
            return False
    
    def _load_config(self) -> bool:
        """Загружает Config из файла"""
        try:
            saved_config = self.persistence.load_config()
            if saved_config:
                self.config_storage.config.CopyFrom(saved_config)
                info("PERSISTENCE", f"{self.log_prefix} Загружен Config из файла (индивидуальные настройки)")
                return True
            else:
                debug("PERSISTENCE", f"{self.log_prefix} Индивидуальные настройки Config не найдены, используются дефолтные из node_defaults.json")
                return False
        except Exception as e:
            error("PERSISTENCE", f"{self.log_prefix} Ошибка загрузки Config: {e}")
            return False
    
    def _load_module_config(self) -> bool:
        """Загружает ModuleConfig из файла"""
        try:
            saved_module_config = self.persistence.load_module_config()
            if saved_module_config:
                self.config_storage.set_module_config(saved_module_config)
                info("PERSISTENCE", f"{self.log_prefix} Загружен ModuleConfig из файла (индивидуальные настройки)")
                return True
            else:
                debug("PERSISTENCE", f"{self.log_prefix} Индивидуальные настройки ModuleConfig не найдены, используются дефолтные из node_defaults.json")
                return False
        except Exception as e:
            error("PERSISTENCE", f"{self.log_prefix} Ошибка загрузки ModuleConfig: {e}")
            return False
    
    def _load_owner(self) -> bool:
        """Загружает Owner из файла"""
        try:
            saved_owner = self.persistence.load_owner()
            if saved_owner:
                self.owner.CopyFrom(saved_owner)
                self.owner.id = self.node_id
                
                # Обновляем public_key если был сгенерирован
                if self.pki_public_key and len(self.pki_public_key) == 32:
                    self.owner.public_key = self.pki_public_key
                
                info("PERSISTENCE", f"{self.log_prefix} Загружен Owner из файла: {self.owner.long_name}/{self.owner.short_name}")
                return True
            else:
                debug("PERSISTENCE", f"{self.log_prefix} Сохраненный Owner не найден, используются значения по умолчанию")
                return False
        except Exception as e:
            error("PERSISTENCE", f"{self.log_prefix} Ошибка загрузки Owner: {e}")
            return False
    
    def _load_canned_messages(self) -> bool:
        """Загружает шаблонные сообщения из файла"""
        try:
            saved_canned_messages = self.persistence.load_canned_messages()
            if saved_canned_messages is not None:
                # Если есть сообщения, включаем модуль (как в firmware)
                if saved_canned_messages:
                    self.config_storage.module_config.canned_message.enabled = True
                debug("PERSISTENCE", f"{self.log_prefix} Загружены шаблонные сообщения")
                return True
            else:
                debug("PERSISTENCE", f"{self.log_prefix} Сохраненные шаблонные сообщения не найдены")
                return False
        except Exception as e:
            error("PERSISTENCE", f"{self.log_prefix} Ошибка загрузки шаблонных сообщений: {e}")
            return False

