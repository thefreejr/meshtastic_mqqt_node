"""
StreamAPI протокол (framing для protobuf)
Из firmware/src/mesh/StreamAPI.cpp
"""

import struct
from typing import Optional

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

