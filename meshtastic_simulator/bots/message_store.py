"""
Хранилище сообщений бота
"""

import time
import threading
from typing import List, Dict, Any, Optional
from datetime import datetime
from ..utils.logger import debug, warn

try:
    from meshtastic import mesh_pb2
    from meshtastic.protobuf import portnums_pb2
except ImportError:
    print("Ошибка: Установите meshtastic: pip install meshtastic")
    raise


class MessageStore:
    """Хранилище сообщений бота"""
    
    def __init__(self, bot_id: str, max_messages: int = 1000):
        """
        Инициализирует хранилище сообщений
        
        Args:
            bot_id: ID бота
            max_messages: Максимальное количество хранимых сообщений
        """
        self.bot_id = bot_id
        self.max_messages = max_messages
        self.messages: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
    
    def add_message(self, packet: mesh_pb2.MeshPacket) -> None:
        """
        Добавляет сообщение в хранилище
        
        Args:
            packet: MeshPacket с сообщением
        """
        try:
            # Проверяем, что это текстовое сообщение
            if packet.WhichOneof('payload_variant') != 'decoded':
                return
            
            if not hasattr(packet.decoded, 'portnum'):
                return
            
            if packet.decoded.portnum != portnums_pb2.PortNum.TEXT_MESSAGE_APP:
                return
            
            # Извлекаем текст сообщения
            message_text = packet.decoded.payload.decode('utf-8', errors='ignore')
            
            # Получаем метаданные
            packet_from = getattr(packet, 'from', 0)
            packet_to = packet.to
            packet_id = packet.id
            channel = packet.channel if packet.channel < 8 else 0
            
            # Получаем время получения
            received_at = time.time()
            if hasattr(packet, 'rx_time') and packet.rx_time > 0:
                received_at = packet.rx_time
            
            # Создаем запись сообщения
            message = {
                'id': packet_id,
                'from': f"!{packet_from:08X}" if packet_from else None,
                'to': f"!{packet_to:08X}" if packet_to != 0xFFFFFFFF else None,
                'message': message_text,
                'received_at': datetime.fromtimestamp(received_at).isoformat(),
                'timestamp': received_at,
                'channel': channel,
                'rssi': getattr(packet, 'rssi', None),
                'snr': getattr(packet, 'snr', None),
                'hop_limit': getattr(packet, 'hop_limit', None),
                'hop_start': getattr(packet, 'hop_start', None)
            }
            
            with self._lock:
                # Добавляем в начало списка (новые сообщения первыми)
                self.messages.insert(0, message)
                
                # Ограничиваем размер хранилища
                if len(self.messages) > self.max_messages:
                    self.messages = self.messages[:self.max_messages]
                
                debug("BOT", f"[{self.bot_id}] Stored message: from={message['from']}, text={message_text[:50]}")
        
        except Exception as e:
            warn("BOT", f"[{self.bot_id}] Error storing message: {e}")
    
    def get_messages(self, limit: int = 50, offset: int = 0, 
                    since: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Получает сообщения с фильтрацией
        
        Args:
            limit: Максимальное количество сообщений
            offset: Смещение для пагинации
            since: Только сообщения после этой даты
            
        Returns:
            Словарь с сообщениями и метаданными
        """
        with self._lock:
            messages = self.messages.copy()
        
        # Фильтрация по дате
        if since:
            since_timestamp = since.timestamp()
            messages = [m for m in messages if m.get('timestamp', 0) >= since_timestamp]
        
        # Применяем пагинацию
        total = len(messages)
        messages = messages[offset:offset + limit]
        
        return {
            'messages': messages,
            'total': total,
            'limit': limit,
            'offset': offset
        }
    
    def clear(self) -> None:
        """Очищает хранилище сообщений"""
        with self._lock:
            self.messages.clear()
            debug("BOT", f"[{self.bot_id}] Cleared message store")


