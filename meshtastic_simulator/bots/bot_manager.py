"""
Менеджер ботов - создание, удаление, управление
"""

import threading
from typing import Dict, Optional, List
from .bot import Bot
from .bot_config import BotConfig
from ..utils.logger import info, warn, error, debug


class BotManager:
    """Менеджер ботов - создание, удаление, управление"""
    
    def __init__(self):
        self.bots: Dict[str, Bot] = {}
        self._lock = threading.Lock()
        self.bot_config = BotConfig()
    
    def load_bots_from_config(self) -> List[str]:
        """
        Загружает ботов из конфигурационных файлов
        
        Returns:
            Список ID загруженных ботов
        """
        loaded_bots = []
        
        try:
            bot_ids = self.bot_config.list_bot_configs()
            
            for bot_id in bot_ids:
                try:
                    config = self.bot_config.load_bot_config(bot_id)
                    if config:
                        bot = Bot(bot_id=bot_id, config=config)
                        if bot.start():
                            with self._lock:
                                self.bots[bot_id] = bot
                            loaded_bots.append(bot_id)
                            info("BOT", f"Loaded bot: {bot_id}")
                        else:
                            error("BOT", f"Failed to start bot: {bot_id}")
                    else:
                        warn("BOT", f"Failed to load config for bot: {bot_id}")
                except Exception as e:
                    error("BOT", f"Error loading bot {bot_id}: {e}")
            
            info("BOT", f"Loaded {len(loaded_bots)} bots from config")
        except Exception as e:
            error("BOT", f"Error loading bots from config: {e}")
        
        return loaded_bots
    
    def create_bot(self, bot_id: str, config: Dict) -> Optional[Bot]:
        """
        Создает нового бота
        
        Args:
            bot_id: ID бота
            config: Конфигурация бота (будет объединена с дефолтами)
            
        Returns:
            Созданный бот или None при ошибке
        """
        with self._lock:
            if bot_id in self.bots:
                error("BOT", f"Bot {bot_id} already exists")
                return None
            
            try:
                # Загружаем дефолтные настройки
                defaults = self.bot_config.load_defaults()
                
                # Объединяем с переданной конфигурацией
                merged_config = self.bot_config._merge_config(defaults, config)
                
                # Сохраняем конфигурацию
                if not self.bot_config.save_bot_config(bot_id, merged_config):
                    error("BOT", f"Failed to save config for bot: {bot_id}")
                    return None
                
                # Создаем бота
                bot = Bot(bot_id=bot_id, config=merged_config)
                
                # Запускаем бота
                if bot.start():
                    self.bots[bot_id] = bot
                    info("BOT", f"Created and started bot: {bot_id}")
                    return bot
                else:
                    error("BOT", f"Failed to start bot: {bot_id}")
                    # Удаляем конфигурацию если не удалось запустить
                    self.bot_config.delete_bot_config(bot_id)
                    return None
            except Exception as e:
                error("BOT", f"Error creating bot {bot_id}: {e}")
                # Удаляем конфигурацию при ошибке
                try:
                    self.bot_config.delete_bot_config(bot_id)
                except:
                    pass
                return None
    
    def delete_bot(self, bot_id: str) -> bool:
        """
        Удаляет бота
        
        Args:
            bot_id: ID бота
            
        Returns:
            True если бот удален
        """
        with self._lock:
            if bot_id not in self.bots:
                warn("BOT", f"Bot {bot_id} not found")
                return False
            
            try:
                bot = self.bots[bot_id]
                
                # Останавливаем бота
                bot.stop()
                
                # Удаляем из словаря
                del self.bots[bot_id]
                
                # Удаляем конфигурацию
                if not self.bot_config.delete_bot_config(bot_id):
                    warn("BOT", f"Failed to delete config for bot: {bot_id}")
                
                info("BOT", f"Deleted bot: {bot_id}")
                return True
            except Exception as e:
                error("BOT", f"Error deleting bot {bot_id}: {e}")
                return False
    
    def get_bot(self, bot_id: str) -> Optional[Bot]:
        """
        Получает бота по ID
        
        Args:
            bot_id: ID бота
            
        Returns:
            Бот или None если не найден
        """
        with self._lock:
            return self.bots.get(bot_id)
    
    def get_all_bots(self) -> Dict[str, Bot]:
        """
        Получает всех ботов
        
        Returns:
            Словарь {bot_id: Bot}
        """
        with self._lock:
            return self.bots.copy()
    
    def get_statistics(self) -> Dict:
        """
        Получает статистику всех ботов
        
        Returns:
            Словарь со статистикой
        """
        with self._lock:
            bots_stats = []
            for bot in self.bots.values():
                try:
                    bots_stats.append(bot.get_statistics())
                except Exception as e:
                    error("BOT", f"Error getting statistics for bot {bot.bot_id}: {e}")
            
            return {
                'total_bots': len(self.bots),
                'active_bots': sum(1 for bot in self.bots.values() if bot.mqtt_client and bot.mqtt_client.connected),
                'bots': bots_stats
            }
    
    def stop_all(self) -> None:
        """Останавливает всех ботов"""
        with self._lock:
            for bot in self.bots.values():
                try:
                    bot.stop()
                except Exception as e:
                    error("BOT", f"Error stopping bot {bot.bot_id}: {e}")
            self.bots.clear()
            info("BOT", "All bots stopped")


