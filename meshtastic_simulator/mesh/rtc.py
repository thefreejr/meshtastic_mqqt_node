"""
Модуль RTC для работы со временем (из firmware/src/gps/RTC.cpp)
"""

import time
from enum import IntEnum
from typing import Optional


class RTCQuality(IntEnum):
    """Качество времени RTC"""
    NONE = 0      # Время не установлено
    DEVICE = 1    # Время из RTC модуля
    FROM_NET = 2  # Время от другого узла
    NTP = 3       # Время из NTP (через WiFi/Ethernet)
    GPS = 4       # Время из GPS (наиболее точное)


class RTCSetResult(IntEnum):
    """Результат установки RTC"""
    NOT_SET = 0
    SUCCESS = 1
    INVALID_TIME = 3
    ERROR = 4


class RTC:
    """Класс для работы со временем (аналог firmware/src/gps/RTC.cpp)"""
    
    def __init__(self):
        self.current_quality = RTCQuality.NONE
        self.time_start_sec = 0  # Время старта (Unix timestamp)
        self.time_start_msec = 0  # Время старта (milliseconds)
    
    def get_quality(self) -> RTCQuality:
        """Возвращает текущее качество времени"""
        return self.current_quality
    
    def perhaps_set_rtc(self, quality: RTCQuality, timestamp: int, force_update: bool = False) -> RTCSetResult:
        """
        Устанавливает время RTC (аналог perhapsSetRTC в firmware)
        
        Args:
            quality: Качество времени (RTCQuality)
            timestamp: Unix timestamp (секунды с 1970-01-01)
            force_update: Принудительно обновить даже если качество хуже
            
        Returns:
            RTCSetResult: Результат установки
        """
        try:
            # Проверка на валидность времени (должно быть после 2020 года)
            BUILD_EPOCH = 1577836800  # 2020-01-01 00:00:00 UTC
            if timestamp < BUILD_EPOCH:
                return RTCSetResult.INVALID_TIME
            
            # Логика как в firmware: обновляем если качество лучше или равно (для GPS/NTP)
            if not force_update:
                # Если качество хуже текущего, не обновляем
                if quality < self.current_quality:
                    return RTCSetResult.NOT_SET
                
                # Если качество такое же, проверяем особые случаи
                if quality == self.current_quality:
                    # GPS время всегда обновляем (даже если уже GPS)
                    if quality == RTCQuality.GPS:
                        pass  # Обновляем
                    # NTP время обновляем если уже установлено NTP (для корректировки дрифта)
                    # В firmware это делается каждые 12 часов, но мы упростим - всегда обновляем
                    elif quality == RTCQuality.NTP:
                        pass  # Обновляем
                    else:
                        # Для других качеств, если такое же - не обновляем
                        return RTCSetResult.NOT_SET
            
            # Устанавливаем время
            self.current_quality = quality
            self.time_start_sec = timestamp
            self.time_start_msec = int(time.time() * 1000)
            
            return RTCSetResult.SUCCESS
        except Exception as e:
            return RTCSetResult.ERROR
    
    def get_time(self, local: bool = False) -> int:
        """
        Возвращает текущее время в секундах с 1970-01-01 (аналог getTime в firmware)
        
        Args:
            local: Использовать локальное время (пока не реализовано)
            
        Returns:
            int: Unix timestamp в секундах
        """
        if self.current_quality == RTCQuality.NONE:
            # Если время не установлено, возвращаем время с момента старта (как в firmware)
            # В firmware возвращается время на основе millis(), но здесь мы используем time.time()
            return int(time.time())
        
        # Вычисляем время на основе начального времени и прошедших миллисекунд
        # (как в firmware: ((millis() - timeStartMsec) / 1000) + zeroOffsetSecs)
        current_msec = int(time.time() * 1000)
        elapsed_sec = (current_msec - self.time_start_msec) // 1000
        return self.time_start_sec + elapsed_sec
    
    def get_valid_time(self, min_quality: RTCQuality, local: bool = False) -> int:
        """
        Возвращает время, если качество >= min_quality (аналог getValidTime в firmware)
        
        Args:
            min_quality: Минимальное качество времени
            local: Использовать локальное время (пока не реализовано)
            
        Returns:
            int: Unix timestamp в секундах, или 0 если качество недостаточно
        """
        if self.current_quality >= min_quality:
            return self.get_time(local)
        return 0
    
    def rtc_name(self, quality: RTCQuality) -> str:
        """Возвращает строковое название качества времени"""
        names = {
            RTCQuality.NONE: "None",
            RTCQuality.DEVICE: "Device",
            RTCQuality.FROM_NET: "FromNet",
            RTCQuality.NTP: "NTP",
            RTCQuality.GPS: "GPS",
        }
        return names.get(quality, "Unknown")


# Глобальный экземпляр RTC
_rtc = RTC()


def get_rtc_quality() -> RTCQuality:
    """Возвращает текущее качество времени"""
    return _rtc.get_quality()


def perhaps_set_rtc(quality: RTCQuality, timestamp: int, force_update: bool = False) -> RTCSetResult:
    """Устанавливает время RTC"""
    return _rtc.perhaps_set_rtc(quality, timestamp, force_update)


def get_time(local: bool = False) -> int:
    """Возвращает текущее время в секундах"""
    return _rtc.get_time(local)


def get_valid_time(min_quality: RTCQuality, local: bool = False) -> int:
    """Возвращает время, если качество >= min_quality"""
    return _rtc.get_valid_time(min_quality, local)

