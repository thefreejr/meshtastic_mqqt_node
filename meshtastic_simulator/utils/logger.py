"""
Утилиты для логирования
"""

import sys
from datetime import datetime
from enum import IntEnum
from typing import Optional, List


class LogLevel(IntEnum):
    """Уровни логирования"""
    DEBUG = 0
    INFO = 1
    WARN = 2
    ERROR = 3
    NONE = 4


class Logger:
    """Класс для логирования с поддержкой уровней и фильтрации категорий"""
    
    def __init__(self, level: LogLevel = LogLevel.INFO, categories: Optional[List[str]] = None):
        self.level = level
        self.categories = categories  # None = все категории, список = только разрешённые
        self.symbols = {
            LogLevel.DEBUG: "🔍️",
            LogLevel.INFO: "ℹ️ ",
            LogLevel.WARN: "⚠️ ",
            LogLevel.ERROR: "❌ ",
        }
    
    def _should_log(self, level: LogLevel, category: str) -> bool:
        """Проверяет, нужно ли логировать сообщение данного уровня и категории"""
        # Проверяем уровень логирования
        if level.value < self.level.value:
            return False
        
        # Проверяем фильтр категорий
        if self.categories is not None:
            # Если список категорий не пуст, разрешаем только указанные категории
            if len(self.categories) > 0 and category not in self.categories:
                return False
        
        return True
    
    def log(self, prefix: str, message: str, level: LogLevel = LogLevel.INFO):
        """Единообразное логирование"""
        if not self._should_log(level, prefix):
            return
        
        timestamp = datetime.now().strftime("%H:%M:%S")
        symbol = self.symbols.get(level, "•")
        
        print(f"[{timestamp}] [{prefix}] {symbol} {message}", file=sys.stdout)
    
    def debug(self, prefix: str, message: str):
        """Логирование уровня DEBUG"""
        self.log(prefix, message, LogLevel.DEBUG)
    
    def info(self, prefix: str, message: str):
        """Логирование уровня INFO"""
        self.log(prefix, message, LogLevel.INFO)
    
    def warn(self, prefix: str, message: str):
        """Логирование уровня WARN"""
        self.log(prefix, message, LogLevel.WARN)
    
    def error(self, prefix: str, message: str):
        """Логирование уровня ERROR"""
        self.log(prefix, message, LogLevel.ERROR)


# Глобальный экземпляр логгера
_logger = Logger()


def set_log_level(level: LogLevel):
    """Устанавливает уровень логирования"""
    global _logger
    _logger.level = level


def get_log_level() -> LogLevel:
    """Возвращает текущий уровень логирования"""
    return _logger.level


def set_log_categories(categories: Optional[List[str]]):
    """Устанавливает фильтр категорий логов
    
    Args:
        categories: None или пустой список = все категории разрешены
                   Список категорий = логировать только указанные категории
    """
    global _logger
    _logger.categories = categories


def get_log_categories() -> Optional[List[str]]:
    """Возвращает текущий фильтр категорий логов"""
    return _logger.categories


def log(prefix: str, message: str, level: LogLevel = LogLevel.INFO):
    """Единообразное логирование (удобная функция для обратной совместимости)"""
    _logger.log(prefix, message, level)


def debug(prefix: str, message: str):
    """Логирование уровня DEBUG"""
    _logger.debug(prefix, message)


def info(prefix: str, message: str):
    """Логирование уровня INFO"""
    _logger.info(prefix, message)


def warn(prefix: str, message: str):
    """Логирование уровня WARN"""
    _logger.warn(prefix, message)


def error(prefix: str, message: str):
    """Логирование уровня ERROR"""
    _logger.error(prefix, message)

