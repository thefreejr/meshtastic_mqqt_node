"""
Загрузка и валидация конфигурации ботов
"""

import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from ..config_loader import ConfigLoader
from ..utils.logger import warn, error, debug


class BotConfig:
    """Загрузка и валидация конфигурации ботов"""
    
    def __init__(self):
        self.config_loader = ConfigLoader.get_instance()
        self.bots_dir = Path(__file__).parent.parent.parent / "config" / "bots"
        self.defaults_file = self.bots_dir / "bot_defaults.yaml"
    
    def load_defaults(self) -> Dict[str, Any]:
        """
        Загружает дефолтные настройки бота
        
        Returns:
            Словарь с дефолтными настройками
        """
        defaults = {}
        try:
            if self.defaults_file.exists():
                with open(self.defaults_file, 'r', encoding='utf-8') as f:
                    defaults = yaml.safe_load(f) or {}
            else:
                warn("BOT", f"Defaults file not found: {self.defaults_file}, using empty defaults")
        except Exception as e:
            error("BOT", f"Error loading bot defaults: {e}")
        
        return defaults.get('bot', {})
    
    def load_bot_config(self, bot_id: str) -> Optional[Dict[str, Any]]:
        """
        Загружает конфигурацию конкретного бота
        
        Args:
            bot_id: ID бота (имя файла без расширения)
            
        Returns:
            Словарь с конфигурацией бота или None если файл не найден
        """
        bot_file = self.bots_dir / f"bot_{bot_id}.yaml"
        
        if not bot_file.exists():
            return None
        
        try:
            with open(bot_file, 'r', encoding='utf-8') as f:
                bot_config = yaml.safe_load(f) or {}
            
            # Объединяем с дефолтными настройками
            defaults = self.load_defaults()
            merged_config = self._merge_config(defaults, bot_config.get('bot', {}))
            
            return merged_config
        except Exception as e:
            error("BOT", f"Error loading bot config for {bot_id}: {e}")
            return None
    
    def save_bot_config(self, bot_id: str, config: Dict[str, Any]) -> bool:
        """
        Сохраняет конфигурацию бота в файл
        
        Args:
            bot_id: ID бота
            config: Конфигурация бота
            
        Returns:
            True если успешно сохранено
        """
        # Создаем директорию если не существует
        self.bots_dir.mkdir(parents=True, exist_ok=True)
        
        bot_file = self.bots_dir / f"bot_{bot_id}.yaml"
        
        try:
            bot_config = {
                'bot_id': bot_id,
                'bot': config
            }
            
            with open(bot_file, 'w', encoding='utf-8') as f:
                yaml.dump(bot_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            
            debug("BOT", f"Saved bot config: {bot_file}")
            return True
        except Exception as e:
            error("BOT", f"Error saving bot config for {bot_id}: {e}")
            return False
    
    def delete_bot_config(self, bot_id: str) -> bool:
        """
        Удаляет конфигурационный файл бота
        
        Args:
            bot_id: ID бота
            
        Returns:
            True если файл удален
        """
        bot_file = self.bots_dir / f"bot_{bot_id}.yaml"
        
        try:
            if bot_file.exists():
                bot_file.unlink()
                debug("BOT", f"Deleted bot config: {bot_file}")
                return True
            return False
        except Exception as e:
            error("BOT", f"Error deleting bot config for {bot_id}: {e}")
            return False
    
    def list_bot_configs(self) -> list:
        """
        Возвращает список всех конфигураций ботов
        
        Returns:
            Список ID ботов
        """
        bot_ids = []
        
        if not self.bots_dir.exists():
            return bot_ids
        
        try:
            for bot_file in self.bots_dir.glob("bot_*.yaml"):
                # Извлекаем bot_id из имени файла (bot_<id>.yaml)
                bot_id = bot_file.stem[4:]  # Убираем "bot_" префикс
                bot_ids.append(bot_id)
        except Exception as e:
            error("BOT", f"Error listing bot configs: {e}")
        
        return bot_ids
    
    def _merge_config(self, defaults: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """
        Рекурсивно объединяет конфигурации (override перезаписывает defaults)
        
        Args:
            defaults: Дефолтные настройки
            override: Переопределяющие настройки
            
        Returns:
            Объединенная конфигурация
        """
        result = defaults.copy()
        
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                # Рекурсивное объединение для вложенных словарей
                result[key] = self._merge_config(result[key], value)
            else:
                # Перезапись значения
                result[key] = value
        
        return result
    
    def get_mqtt_config(self, bot_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Получает MQTT конфигурацию для бота (объединяет с project.yaml)
        
        Args:
            bot_config: Конфигурация бота
            
        Returns:
            Словарь с MQTT настройками
        """
        project_config = self.config_loader.get_config()
        mqtt_defaults = project_config.get('mqtt', {})
        
        bot_mqtt = bot_config.get('mqtt', {})
        
        # Используем настройки бота если указаны, иначе из project.yaml
        return {
            'address': bot_mqtt.get('address') or mqtt_defaults.get('address', 'mqtt.meshtastic.org'),
            'port': bot_mqtt.get('port') or mqtt_defaults.get('port', 1883),
            'username': bot_mqtt.get('username') or mqtt_defaults.get('username', 'meshdev'),
            'password': bot_mqtt.get('password') or mqtt_defaults.get('password', 'large4cats'),
            'root': bot_mqtt.get('root') or mqtt_defaults.get('root', 'msh')
        }


