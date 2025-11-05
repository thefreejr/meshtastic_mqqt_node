"""
Управление каналами (из firmware/src/mesh/Channels.cpp)
"""

from typing import Optional
from meshtastic.protobuf import channel_pb2
from ..config import MAX_NUM_CHANNELS


class Channels:
    """Управление каналами (из firmware/src/mesh/Channels.cpp)"""
    
    def __init__(self):
        self.channels = []
        self.hashes = {}  # Кэш для hash каналов (как hashes[] в firmware)
        self.init_defaults()
    
    def _xor_hash(self, data: bytes) -> int:
        """Вычисляет XOR hash (как xorHash в firmware)"""
        result = 0
        for byte in data:
            result ^= byte
        return result & 0xFF
    
    def _get_key(self, ch_index: int) -> Optional[bytes]:
        """Возвращает расширенный PSK ключ для канала (как Channels::getKey)"""
        ch = self.get_by_index(ch_index)
        
        if ch.role == channel_pb2.Channel.Role.DISABLED:
            return None
        
        psk = ch.settings.psk
        if len(psk) == 0:
            # Если PSK пустой, используем PRIMARY ключ для SECONDARY каналов
            if ch.role == channel_pb2.Channel.Role.SECONDARY:
                return self._get_key(0)  # Используем PRIMARY ключ
            return None
        elif len(psk) == 1:
            # Расширяем alias (как в firmware: psk.size == 1)
            psk_index = psk[0]
            if psk_index == 0:
                return None  # Отключено шифрование
            else:
                # Расширяем до defaultpsk (как в firmware)
                defaultpsk = bytes([0xd4, 0xf1, 0xbb, 0x3a, 0x20, 0x29, 0x07, 0x59,
                                   0xf0, 0xbc, 0xff, 0xab, 0xcf, 0x4e, 0x69, 0x01])
                expanded_psk = bytearray(defaultpsk)
                # Bump up the last byte as needed (как в firmware: *last = *last + pskIndex - 1)
                expanded_psk[15] = (expanded_psk[15] + psk_index - 1) & 0xFF
                return bytes(expanded_psk)
        elif len(psk) < 16:
            # Дополняем нулями до 16 байт (AES128)
            return psk + b'\x00' * (16 - len(psk))
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
    
    def init_defaults(self):
        """Инициализирует каналы по умолчанию (как в Channels::initDefaults)"""
        for i in range(MAX_NUM_CHANNELS):
            channel = channel_pb2.Channel()
            channel.index = i
            self.init_default_channel(channel, i)
            self.channels.append(channel)
    
    def init_default_channel(self, channel: channel_pb2.Channel, ch_index: int):
        """Инициализирует канал по умолчанию (как в Channels::initDefaultChannel)"""
        defaultpsk_index = 1
        
        if ch_index == 0:
            # PRIMARY канал - публичный по умолчанию
            defaultpsk = bytes([0xd4, 0xf1, 0xbb, 0x3a, 0x20, 0x29, 0x07, 0x59,
                               0xf0, 0xbc, 0xff, 0xab, 0xcf, 0x4e, 0x69, 0x01])
            channel.settings.psk = defaultpsk  # Полный PSK (16 байт)
            channel.settings.name = "LongFast"
            channel.settings.uplink_enabled = True
            channel.settings.downlink_enabled = True
            channel.settings.module_settings.position_precision = 13
            channel.role = channel_pb2.Channel.Role.PRIMARY
        else:
            # SECONDARY каналы - приватные по умолчанию
            channel.settings.psk = bytes([defaultpsk_index])
            channel.settings.name = ""
            channel.settings.uplink_enabled = False
            channel.settings.downlink_enabled = False
            channel.role = channel_pb2.Channel.Role.SECONDARY
    
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
    
    def set_channel(self, channel: channel_pb2.Channel):
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
        print(f"📝 Канал {channel.index} обновлен: role={channel.role}, name={channel.settings.name}, hash={self.get_hash(channel.index)}")
    
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

