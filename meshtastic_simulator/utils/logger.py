"""
Утилиты для логирования
"""

import os
import sys
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import List, Optional


class LogLevel(IntEnum):
    """Уровни логирования"""
    DEBUG = 0
    INFO = 1
    WARN = 2
    ERROR = 3
    NONE = 4


class Logger:
    """Класс для логирования с поддержкой уровней и фильтрации категорий"""
    
    def __init__(self, level: LogLevel = LogLevel.INFO, categories: Optional[List[str]] = None, log_file: Optional[str] = None) -> None:
        self.level = level
        self.categories = categories  # None = все категории, список = только разрешённые
        self.log_file = log_file  # Путь к файлу для логирования (None = только stdout)
        self.log_file_handle = None
        self.symbols = {
            LogLevel.DEBUG: "🔍️",
            LogLevel.INFO: "ℹ️ ",
            LogLevel.WARN: "⚠️ ",
            LogLevel.ERROR: "❌ ",
        }
        
        # Открываем файл для логирования если указан
        if self.log_file:
            self._open_log_file()
    
    def _open_log_file(self) -> None:
        """Открывает файл для логирования"""
        try:
            log_path = Path(self.log_file)
            # Создаем директорию если её нет
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # Открываем файл в режиме append
            self.log_file_handle = open(log_path, 'a', encoding='utf-8', buffering=1)  # line buffering
        except Exception as e:
            print(f"⚠️  Ошибка открытия файла логов {self.log_file}: {e}", file=sys.stderr)
            self.log_file_handle = None
    
    def _close_log_file(self) -> None:
        """Закрывает файл логирования"""
        if self.log_file_handle:
            try:
                self.log_file_handle.close()
            except:
                pass
            self.log_file_handle = None
    
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
        log_message = f"[{timestamp}] [{prefix}] {symbol} {message}"
        
        # Выводим в stdout
        print(log_message, file=sys.stdout)
        
        # Записываем в файл если указан
        if self.log_file_handle:
            try:
                self.log_file_handle.write(log_message + '\n')
                self.log_file_handle.flush()
            except Exception as e:
                # Если ошибка записи в файл, выводим предупреждение один раз
                if not hasattr(self, '_file_error_logged'):
                    print(f"⚠️  Ошибка записи в файл логов: {e}", file=sys.stderr)
                    self._file_error_logged = True
    
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


def set_log_level(level: LogLevel) -> None:
    """Устанавливает уровень логирования"""
    global _logger
    _logger.level = level


def set_log_file(log_file: Optional[str]):
    """Устанавливает файл для логирования
    
    Args:
        log_file: Путь к файлу для логирования (None = только stdout)
    """
    global _logger
    # Закрываем старый файл если был открыт
    if _logger.log_file_handle:
        _logger._close_log_file()
    
    _logger.log_file = log_file
    if log_file:
        _logger._open_log_file()


def get_log_level() -> LogLevel:
    """Возвращает текущий уровень логирования"""
    return _logger.level


def set_log_categories(categories: Optional[List[str]]) -> None:
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

