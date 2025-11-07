"""
Управление каналами (из firmware/src/mesh/Channels.cpp)
Загружает дефолтные каналы из config/node_defaults.json
"""

import json
from pathlib import Path
from typing import Optional

from meshtastic.protobuf import channel_pb2

from ..config import MAX_NUM_CHANNELS


class Channels:
    """Управление каналами (из firmware/src/mesh/Channels.cpp)"""
    
    def __init__(self) -> None:
        self.channels = []
        self.hashes = {}  # Кэш для hash каналов (как hashes[] в firmware)
        # Загружаем дефолтные каналы из файла
        self._load_default_channels_from_file()
    
    def _xor_hash(self, data: bytes) -> int:
        """Вычисляет XOR hash (как xorHash в firmware)"""
        result = 0
        for byte in data:
            result ^= byte
        return result & 0xFF
    
    def _get_primary_index(self) -> int:
        """Находит индекс PRIMARY канала"""
        for i in range(MAX_NUM_CHANNELS):
            ch = self.get_by_index(i)
            if ch.role == channel_pb2.Channel.Role.PRIMARY:
                return i
        return 0  # Fallback на индекс 0 если PRIMARY не найден
    
    def _get_key(self, ch_index: int, _visited: Optional[set] = None) -> Optional[bytes]:
        """
        Возвращает расширенный PSK ключ для канала (как Channels::getKey)
        
        Args:
            ch_index: Индекс канала
            _visited: Множество посещенных индексов для предотвращения бесконечной рекурсии
        
        Returns:
            Расширенный PSK ключ (16 или 32 байта) или None если шифрование отключено
        """
        if _visited is None:
            _visited = set()
        
        # Защита от бесконечной рекурсии
        if ch_index in _visited:
            return None
        _visited.add(ch_index)
        
        ch = self.get_by_index(ch_index)
        
        # Проверка на DISABLED канал (как в firmware: ch.role == DISABLED)
        if ch.role == channel_pb2.Channel.Role.DISABLED:
            return None
        
        # Проверка has_settings (в firmware: !ch.has_settings)
        # В Python protobuf это проверяется через наличие settings
        if not hasattr(ch, 'settings') or not ch.settings:
            return None
        
        psk = ch.settings.psk
        
        # Если PSK пустой
        if len(psk) == 0:
            # Если PSK пустой, используем PRIMARY ключ для SECONDARY каналов
            if ch.role == channel_pb2.Channel.Role.SECONDARY:
                primary_idx = self._get_primary_index()
                if primary_idx != ch_index:  # Избегаем рекурсии если PRIMARY == текущий канал
                    return self._get_key(primary_idx, _visited)
            return None  # Шифрование отключено
        
        # Если PSK состоит из 1 байта (alias)
        elif len(psk) == 1:
            psk_index = psk[0]
            if psk_index == 0:
                return None  # Отключено шифрование (как в firmware: k.length = 0)
            else:
                # Расширяем до defaultpsk (как в firmware)
                defaultpsk = bytes([0xd4, 0xf1, 0xbb, 0x3a, 0x20, 0x29, 0x07, 0x59,
                                   0xf0, 0xbc, 0xff, 0xab, 0xcf, 0x4e, 0x69, 0x01])
                expanded_psk = bytearray(defaultpsk)
                # Bump up the last byte as needed (как в firmware: *last = *last + pskIndex - 1)
                # index of 1 means no change vs defaultPSK
                expanded_psk[15] = (expanded_psk[15] + psk_index - 1) & 0xFF
                return bytes(expanded_psk)
        
        # Если PSK короче 16 байт - дополняем нулями до AES128
        elif len(psk) < 16:
            # Дополняем нулями до 16 байт (AES128)
            return psk + b'\x00' * (16 - len(psk))
        
        # Если PSK между 16 и 32 байтами (но не ровно 16) - дополняем до AES256
        elif len(psk) < 32 and len(psk) != 16:
            # Дополняем нулями до 32 байт (AES256)
            return psk + b'\x00' * (32 - len(psk))
        
        # Если PSK ровно 16 байт (AES128) или 32 байта (AES256) - возвращаем как есть
        else:
            return psk
    
    def generate_hash(self, ch_index: int) -> int:
        """Вычисляет hash канала (как Channels::generateHash)"""
        # hash = xorHash(channel_name) XOR xorHash(PSK_bytes)
        channel_name = self.get_global_id(ch_index)
        key = self._get_key(ch_index)
        
        if key is None:
            return -1
        
        name_hash = self._xor_hash(channel_name.encode('utf-8'))
        psk_hash = self._xor_hash(key)
        channel_hash = name_hash ^ psk_hash
        
        return channel_hash & 0xFF
    
    def get_hash(self, ch_index: int) -> int:
        """Возвращает hash канала (кэшированный)"""
        if ch_index not in self.hashes:
            self.hashes[ch_index] = self.generate_hash(ch_index)
        return self.hashes[ch_index]
    
    def decrypt_for_hash(self, ch_index: int, channel_hash: int) -> bool:
        """Проверяет, можно ли использовать канал для расшифровки по hash (как Channels::decryptForHash)"""
        if ch_index >= len(self.channels):
            return False
        if self.get_hash(ch_index) != channel_hash:
            return False
        return True
    
    def _load_default_channels_from_file(self) -> None:
        """Загружает дефолтные каналы из config/node_defaults.json"""
        from .persistence import Persistence
        
        NODE_DEFAULTS_FILE = Path(__file__).parent.parent.parent / "config" / "node_defaults.json"
        
        try:
            if not NODE_DEFAULTS_FILE.exists():
                # Файл не существует - создаем дефолтные каналы программно
                self._create_default_channels()
                return
            
            with open(NODE_DEFAULTS_FILE, 'r', encoding='utf-8') as f:
                defaults = json.load(f)
            
            if 'channels' in defaults and defaults['channels']:
                # Загружаем каналы из файла
                persistence = Persistence()
                self.channels = [persistence._dict_to_channel(ch_data) for ch_data in defaults['channels']]
                # Убеждаемся, что у всех каналов правильный index
                for i, ch in enumerate(self.channels):
                    ch.index = i
            else:
                # Каналы не найдены в файле - создаем дефолтные
                self._create_default_channels()
        except Exception as e:
            # Если ошибка загрузки - создаем дефолтные каналы
            self._create_default_channels()
    
    def _create_default_channels(self) -> None:
        """Создает дефолтные каналы программно (используется только при отсутствии файла)"""
        self.channels = []
        for i in range(MAX_NUM_CHANNELS):
            channel = channel_pb2.Channel()
            channel.index = i
            
            if i == 0:
                # PRIMARY канал - публичный по умолчанию
                defaultpsk = bytes([0xd4, 0xf1, 0xbb, 0x3a, 0x20, 0x29, 0x07, 0x59,
                                   0xf0, 0xbc, 0xff, 0xab, 0xcf, 0x4e, 0x69, 0x01])
                channel.settings.psk = defaultpsk
                channel.settings.name = "LongFast"
                channel.settings.uplink_enabled = True
                channel.settings.downlink_enabled = True
                channel.settings.module_settings.position_precision = 13
                channel.role = channel_pb2.Channel.Role.PRIMARY
            else:
                # Все остальные каналы (index 1-7) - DISABLED по умолчанию
                channel.settings.psk = b""
                channel.settings.name = ""
                channel.settings.uplink_enabled = False
                channel.settings.downlink_enabled = False
                channel.role = channel_pb2.Channel.Role.DISABLED
            
            self.channels.append(channel)
    
    def get_by_index(self, ch_index: int) -> channel_pb2.Channel:
        """Возвращает канал по индексу (как Channels::getByIndex)"""
        if 0 <= ch_index < len(self.channels):
            return self.channels[ch_index]
        # Возвращаем пустой DISABLED канал
        channel = channel_pb2.Channel()
        channel.index = -1
        channel.role = channel_pb2.Channel.Role.DISABLED
        return channel
    
    def get_num_channels(self) -> int:
        """Возвращает количество каналов"""
        return MAX_NUM_CHANNELS
    
    def set_channel(self, channel: channel_pb2.Channel) -> None:
        """Устанавливает канал (как Channels::setChannel)"""
        if channel.index < 0 or channel.index >= MAX_NUM_CHANNELS:
            raise ValueError(f"Invalid channel index: {channel.index}")
        
        # Если это новый PRIMARY, делаем остальные SECONDARY
        if channel.role == channel_pb2.Channel.Role.PRIMARY:
            for i in range(MAX_NUM_CHANNELS):
                if self.channels[i].role == channel_pb2.Channel.Role.PRIMARY:
                    self.channels[i].role = channel_pb2.Channel.Role.SECONDARY
        
        # Обновляем канал
        self.channels[channel.index].CopyFrom(channel)
        # Пересчитываем hash при изменении канала
        if channel.index in self.hashes:
            del self.hashes[channel.index]
        print(f"📝 Channel {channel.index} updated: role={channel.role}, name={channel.settings.name}, hash={self.get_hash(channel.index)}")
    
    def any_mqtt_enabled(self) -> bool:
        """Проверяет есть ли каналы с uplink или downlink (как Channels::anyMqttEnabled)"""
        for i in range(MAX_NUM_CHANNELS):
            ch = self.channels[i]
            if (ch.role != channel_pb2.Channel.Role.DISABLED and 
                (ch.settings.downlink_enabled or ch.settings.uplink_enabled)):
                return True
        return False
    
    def get_global_id(self, ch_index: int) -> str:
        """Возвращает глобальный ID канала (как Channels::getGlobalId/getName)"""
        if 0 <= ch_index < len(self.channels):
            name = self.channels[ch_index].settings.name
            if name:
                return name
            if ch_index == 0:
                return "LongFast"
            else:
                return "Custom"
        return ""
    
    def get_by_name(self, ch_name: str) -> channel_pb2.Channel:
        """Возвращает канал по имени (как Channels::getByName)"""
        for i in range(MAX_NUM_CHANNELS):
            if self.get_global_id(i).lower() == ch_name.lower():
                return self.channels[i]
        return self.channels[0] if self.channels else channel_pb2.Channel()

