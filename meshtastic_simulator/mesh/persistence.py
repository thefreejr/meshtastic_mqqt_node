"""
Модуль для сохранения и загрузки настроек узла (из firmware/src/mesh/NodeDB.cpp)
"""

import json
import base64
import os
from pathlib import Path
from typing import Optional, Dict, Any
from meshtastic import mesh_pb2
from meshtastic.protobuf import config_pb2, module_config_pb2, channel_pb2
from ..utils.logger import debug, info, warn, error

# Имя файла для сохранения настроек
SETTINGS_FILE = "node_settings.json"


class Persistence:
    """Класс для сохранения и загрузки настроек узла"""
    
    def __init__(self, settings_dir: Optional[str] = None):
        """
        Инициализирует модуль сохранения настроек
        
        Args:
            settings_dir: Директория для сохранения настроек (по умолчанию текущая)
        """
        if settings_dir:
            self.settings_path = Path(settings_dir) / SETTINGS_FILE
        else:
            self.settings_path = Path(SETTINGS_FILE)
        debug("PERSISTENCE", f"Файл настроек: {self.settings_path}")
    
    def _protobuf_to_dict(self, pb_msg) -> Dict[str, Any]:
        """Преобразует protobuf сообщение в словарь (сериализует в base64)"""
        try:
            serialized = pb_msg.SerializeToString()
            return {
                "_type": pb_msg.DESCRIPTOR.full_name,
                "_data": base64.b64encode(serialized).decode('utf-8')
            }
        except Exception as e:
            error("PERSISTENCE", f"Ошибка сериализации protobuf: {e}")
            return {}
    
    def _dict_to_protobuf(self, data: Dict[str, Any], pb_class):
        """Преобразует словарь обратно в protobuf сообщение"""
        try:
            if "_type" not in data or "_data" not in data:
                return None
            serialized = base64.b64decode(data["_data"])
            pb_msg = pb_class()
            pb_msg.ParseFromString(serialized)
            return pb_msg
        except Exception as e:
            error("PERSISTENCE", f"Ошибка десериализации protobuf: {e}")
            return None
    
    def _channel_to_dict(self, channel: channel_pb2.Channel) -> Dict[str, Any]:
        """Преобразует канал в словарь"""
        try:
            psk_b64 = ""
            if channel.settings.psk:
                psk_b64 = base64.b64encode(channel.settings.psk).decode('utf-8')
            
            radio_value = None
            try:
                if channel.settings.HasField('radio'):
                    radio_value = channel.settings.radio
            except Exception:
                # radio может не иметь presence
                pass
            
            return {
                "index": channel.index,
                "role": channel.role,
                "name": channel.settings.name if channel.settings.name else "",
                "psk": psk_b64,
                "radio": radio_value,
                "uplink_enabled": channel.settings.uplink_enabled,
                "downlink_enabled": channel.settings.downlink_enabled,
            }
        except Exception as e:
            error("PERSISTENCE", f"Ошибка преобразования канала в словарь: {e}")
            return {
                "index": channel.index if hasattr(channel, 'index') else 0,
                "role": channel.role if hasattr(channel, 'role') else channel_pb2.Channel.Role.DISABLED,
                "name": "",
                "psk": "",
                "radio": None,
                "uplink_enabled": True,
                "downlink_enabled": True,
            }
    
    def _dict_to_channel(self, data: Dict[str, Any]) -> channel_pb2.Channel:
        """Преобразует словарь обратно в канал"""
        channel = channel_pb2.Channel()
        channel.index = data.get("index", 0)
        # Убеждаемся, что index установлен правильно
        if channel.index < 0:
            channel.index = 0
        channel.role = data.get("role", channel_pb2.Channel.Role.DISABLED)
        channel.settings.name = data.get("name", "")
        psk_b64 = data.get("psk", "")
        if psk_b64:
            try:
                channel.settings.psk = base64.b64decode(psk_b64)
            except Exception as e:
                warn("PERSISTENCE", f"Ошибка декодирования PSK для канала {channel.index}: {e}")
                channel.settings.psk = b""
        if data.get("radio") is not None:
            try:
                channel.settings.radio = data["radio"]
            except Exception as e:
                debug("PERSISTENCE", f"Не удалось установить radio для канала {channel.index}: {e}")
        channel.settings.uplink_enabled = data.get("uplink_enabled", True)
        channel.settings.downlink_enabled = data.get("downlink_enabled", True)
        # Убеждаемся, что has_settings установлен
        # В protobuf has_settings проверяется через HasField, но для совместимости устанавливаем
        return channel
    
    def save_channels(self, channels: list) -> bool:
        """Сохраняет каналы в файл"""
        try:
            settings = self._load_settings()
            settings["channels"] = [self._channel_to_dict(ch) for ch in channels]
            return self._save_settings(settings)
        except Exception as e:
            error("PERSISTENCE", f"Ошибка сохранения каналов: {e}")
            return False
    
    def load_channels(self) -> Optional[list]:
        """Загружает каналы из файла"""
        try:
            settings = self._load_settings()
            if "channels" not in settings:
                return None
            return [self._dict_to_channel(ch_data) for ch_data in settings["channels"]]
        except Exception as e:
            error("PERSISTENCE", f"Ошибка загрузки каналов: {e}")
            return None
    
    def save_config(self, config: config_pb2.Config) -> bool:
        """Сохраняет Config в файл"""
        try:
            settings = self._load_settings()
            settings["config"] = self._protobuf_to_dict(config)
            return self._save_settings(settings)
        except Exception as e:
            error("PERSISTENCE", f"Ошибка сохранения Config: {e}")
            return False
    
    def load_config(self) -> Optional[config_pb2.Config]:
        """Загружает Config из файла"""
        try:
            settings = self._load_settings()
            if "config" not in settings:
                return None
            return self._dict_to_protobuf(settings["config"], config_pb2.Config)
        except Exception as e:
            error("PERSISTENCE", f"Ошибка загрузки Config: {e}")
            return None
    
    def save_module_config(self, module_config: module_config_pb2.ModuleConfig) -> bool:
        """Сохраняет ModuleConfig в файл"""
        try:
            settings = self._load_settings()
            settings["module_config"] = self._protobuf_to_dict(module_config)
            return self._save_settings(settings)
        except Exception as e:
            error("PERSISTENCE", f"Ошибка сохранения ModuleConfig: {e}")
            return False
    
    def load_module_config(self) -> Optional[module_config_pb2.ModuleConfig]:
        """Загружает ModuleConfig из файла"""
        try:
            settings = self._load_settings()
            if "module_config" not in settings:
                return None
            return self._dict_to_protobuf(settings["module_config"], module_config_pb2.ModuleConfig)
        except Exception as e:
            error("PERSISTENCE", f"Ошибка загрузки ModuleConfig: {e}")
            return None
    
    def save_owner(self, owner: mesh_pb2.User) -> bool:
        """Сохраняет Owner в файл"""
        try:
            settings = self._load_settings()
            settings["owner"] = self._protobuf_to_dict(owner)
            return self._save_settings(settings)
        except Exception as e:
            error("PERSISTENCE", f"Ошибка сохранения Owner: {e}")
            return False
    
    def load_owner(self) -> Optional[mesh_pb2.User]:
        """Загружает Owner из файла"""
        try:
            settings = self._load_settings()
            if "owner" not in settings:
                return None
            return self._dict_to_protobuf(settings["owner"], mesh_pb2.User)
        except Exception as e:
            error("PERSISTENCE", f"Ошибка загрузки Owner: {e}")
            return None
    
    def _load_settings(self) -> Dict[str, Any]:
        """Загружает настройки из файла"""
        if not self.settings_path.exists():
            return {}
        
        try:
            with open(self.settings_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            warn("PERSISTENCE", f"Ошибка чтения файла настроек: {e}")
            return {}
    
    def save_canned_messages(self, messages: str) -> bool:
        """Сохраняет шаблонные сообщения в файл"""
        try:
            settings = self._load_settings()
            settings["canned_messages"] = messages
            return self._save_settings(settings)
        except Exception as e:
            error("PERSISTENCE", f"Ошибка сохранения шаблонных сообщений: {e}")
            return False
    
    def load_canned_messages(self) -> Optional[str]:
        """Загружает шаблонные сообщения из файла"""
        try:
            settings = self._load_settings()
            if "canned_messages" not in settings:
                return None
            return settings["canned_messages"]
        except Exception as e:
            error("PERSISTENCE", f"Ошибка загрузки шаблонных сообщений: {e}")
            return None
    
    def _save_settings(self, settings: Dict[str, Any]) -> bool:
        """Сохраняет настройки в файл"""
        try:
            # Создаем резервную копию если файл существует
            if self.settings_path.exists():
                backup_path = self.settings_path.with_suffix('.json.bak')
                try:
                    import shutil
                    shutil.copy2(self.settings_path, backup_path)
                except Exception:
                    pass  # Игнорируем ошибки резервного копирования
            
            # Сохраняем настройки
            with open(self.settings_path, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
            
            info("PERSISTENCE", f"Настройки сохранены в {self.settings_path}")
            return True
        except Exception as e:
            error("PERSISTENCE", f"Ошибка записи файла настроек: {e}")
            return False

