"""
StreamAPI протокол (framing для protobuf)
Из firmware/src/mesh/StreamAPI.cpp
"""

import struct
import time
from typing import Optional, Callable

from ..config import START1, START2, HEADER_LEN, MAX_TO_FROM_RADIO_SIZE


class StreamAPI:
    """Обработка StreamAPI протокола (framing для protobuf)"""
    
    @staticmethod
    def add_framing(payload: bytes) -> bytes:
        """Добавляет заголовок к protobuf сообщению (0x94C3 + длина)"""
        if len(payload) > MAX_TO_FROM_RADIO_SIZE:
            raise ValueError(f"Payload слишком большой: {len(payload)} > {MAX_TO_FROM_RADIO_SIZE}")
        header = struct.pack('>BBH', START1, START2, len(payload))
        return header + payload
    
    @staticmethod
    def remove_framing(data: bytes) -> Optional[bytes]:
        """Удаляет заголовок и возвращает payload"""
        if len(data) < HEADER_LEN:
            return None
        if data[0] != START1 or data[1] != START2:
            return None
        length = struct.unpack('>H', data[2:4])[0]
        if length > MAX_TO_FROM_RADIO_SIZE:
            return None
        if len(data) < HEADER_LEN + length:
            return None
        return data[HEADER_LEN:HEADER_LEN + length]


class StreamAPIStateMachine:
    """
    State machine для парсинга StreamAPI пакетов (как в firmware StreamAPI::handleRecStream)
    Использует подход firmware с указателем rxPtr вместо буферного поиска
    """
    
    def __init__(self, handle_to_radio_callback: Callable[[bytes], None]):
        """
        Инициализирует state machine
        
        Args:
            handle_to_radio_callback: Callback для обработки распарсенных пакетов
        """
        self.handle_to_radio = handle_to_radio_callback
        self.rx_ptr = 0  # Указатель на текущую позицию в пакете (как rxPtr в firmware)
        self.rx_buf = bytearray(MAX_TO_FROM_RADIO_SIZE + HEADER_LEN)  # Буфер для пакета
        self.last_rx_msec = 0  # Время последнего получения данных (в миллисекундах)
    
    def handle_rec_stream(self, buf: bytes) -> None:
        """
        Обрабатывает входящие байты (как в firmware StreamAPI::handleRecStream)
        
        Args:
            buf: Буфер с входящими байтами
        """
        index = 0
        while index < len(buf):
            c = buf[index]
            index += 1
            
            # Используем указатель для state machine (как в firmware)
            ptr = self.rx_ptr
            
            self.rx_ptr += 1  # Предполагаем, что продвинемся
            self.rx_buf[ptr] = c  # Сохраняем байт (включая framing)
            
            if ptr == 0:  # Ищем START1
                if c != START1:
                    self.rx_ptr = 0  # Не нашли framing, начинаем заново
            elif ptr == 1:  # Ищем START2
                if c != START2:
                    self.rx_ptr = 0  # Не нашли framing, начинаем заново
            elif ptr >= HEADER_LEN - 1:  # У нас есть как минимум 4 байта framing
                # Извлекаем длину (big-endian 16-bit)
                length = (self.rx_buf[2] << 8) + self.rx_buf[3]
                
                if ptr == HEADER_LEN - 1:
                    # Мы только что закончили 4-байтовый заголовок, проверяем длину
                    if length > MAX_TO_FROM_RADIO_SIZE:
                        self.rx_ptr = 0  # Длина некорректна, начинаем поиск заново
                
                if self.rx_ptr != 0:  # Пакет все еще считается 'хорошим'?
                    if ptr + 1 >= length + HEADER_LEN:  # Получили весь payload?
                        self.rx_ptr = 0  # Начинаем заново для следующего пакета
                        
                        # Если пакет не провалился и у нас правильное количество байтов, парсим его
                        try:
                            payload = bytes(self.rx_buf[HEADER_LEN:HEADER_LEN + length])
                            self.handle_to_radio(payload)
                        except Exception as e:
                            # Ошибка обработки пакета - логируем, но не закрываем соединение
                            # (как в firmware - ошибки парсинга просто сбрасывают rxPtr)
                            from ..utils.logger import error
                            error("StreamAPI", f"Error processing packet (ignoring): {e}")
        
        # Обновляем время последнего получения данных
        self.last_rx_msec = int(time.time() * 1000)
    
    def reset(self) -> None:
        """Сбрасывает состояние state machine"""
        self.rx_ptr = 0
    
    def get_last_rx_msec(self) -> int:
        """Возвращает время последнего получения данных в миллисекундах"""
        return self.last_rx_msec

