"""
Обработчик MeshPacket - общая логика для TCP и MQTT
"""

import random
from typing import Optional

try:
    from meshtastic import mesh_pb2
    from meshtastic.protobuf import portnums_pb2
except ImportError:
    print("Error: Install meshtastic: pip install meshtastic")
    raise

from ..config import MAX_NUM_CHANNELS, DEFAULT_HOP_LIMIT
from ..utils.logger import debug


class PacketHandler:
    """Обработчик MeshPacket - общая логика обработки пакетов"""
    
    @staticmethod
    def prepare_outgoing_packet(packet: mesh_pb2.MeshPacket) -> None:
        """
        Подготавливает исходящий пакет (устанавливает hop_limit, hop_start, id)
        
        Args:
            packet: MeshPacket для подготовки
        """
        # Генерируем ID если не установлен
        if packet.id == 0:
            packet.id = random.randint(1, 0xFFFFFFFF)
        
        # Устанавливаем hop_limit и hop_start для пакетов от клиента
        want_ack = getattr(packet, 'want_ack', False)
        hop_limit = getattr(packet, 'hop_limit', 0)
        
        if want_ack and hop_limit == 0:
            hop_limit = DEFAULT_HOP_LIMIT
            packet.hop_limit = hop_limit
            debug("TCP", f"Set default hop_limit={hop_limit} for packet with want_ack=True")
        
        if hop_limit > 0:
            hop_start = getattr(packet, 'hop_start', 0)
            if hop_start == 0:
                packet.hop_start = hop_limit
                debug("TCP", f"Set hop_start={hop_limit} for outgoing packet")
    
    @staticmethod
    def should_send_ack(packet: mesh_pb2.MeshPacket) -> bool:
        """
        Проверяет, нужно ли отправлять ACK для пакета
        
        Args:
            packet: MeshPacket для проверки
            
        Returns:
            True если нужно отправить ACK, False иначе
        """
        want_ack = getattr(packet, 'want_ack', False)
        payload_type = packet.WhichOneof('payload_variant')
        
        if not (want_ack and payload_type == 'decoded'):
            return False
        
        # Проверяем, что это не ACK сам по себе (Routing сообщение с request_id)
        is_routing_ack = (
            hasattr(packet.decoded, 'portnum') and 
            packet.decoded.portnum == portnums_pb2.PortNum.ROUTING_APP and
            hasattr(packet.decoded, 'request_id') and 
            packet.decoded.request_id != 0
        )
        
        return not is_routing_ack
    
    @staticmethod
    def create_ack_packet(original_packet: mesh_pb2.MeshPacket, 
                          our_node_num: int, 
                          channel_index: int,
                          error_reason: int = None) -> mesh_pb2.MeshPacket:
        """
        Создает ACK/NAK пакет для исходного пакета
        
        Args:
            original_packet: Исходный пакет, для которого создается ACK/NAK
            our_node_num: Наш node_num (для поля from)
            channel_index: Индекс канала
            error_reason: Причина ошибки для NAK (Routing.Error), если None - ACK (NONE)
            
        Returns:
            MeshPacket с ACK/NAK
        """
        packet_from = getattr(original_packet, 'from', 0)
        packet_id = original_packet.id
        hop_limit = 0  # TCP клиент - прямое соединение
        
        # Создаем Routing сообщение с ACK/NAK
        routing_msg = mesh_pb2.Routing()
        if error_reason is None:
            routing_msg.error_reason = mesh_pb2.Routing.Error.NONE  # ACK = нет ошибки
        else:
            routing_msg.error_reason = error_reason  # NAK = ошибка
        
        # Создаем MeshPacket с ACK
        ack_packet = mesh_pb2.MeshPacket()
        ack_packet.id = random.randint(1, 0xFFFFFFFF)
        ack_packet.to = packet_from
        setattr(ack_packet, 'from', our_node_num)
        ack_packet.channel = channel_index
        ack_packet.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        ack_packet.decoded.request_id = packet_id  # КРИТИЧЕСКИ ВАЖНО: ID исходного пакета
        ack_packet.decoded.payload = routing_msg.SerializeToString()
        ack_packet.priority = mesh_pb2.MeshPacket.Priority.ACK
        ack_packet.hop_limit = hop_limit
        ack_packet.want_ack = False  # ACK на ACK не нужен
        ack_packet.hop_start = hop_limit
        
        return ack_packet
    
    @staticmethod
    def get_hops_away(packet: mesh_pb2.MeshPacket) -> int:
        """
        Вычисляет количество hops away для пакета
        
        Args:
            packet: MeshPacket для анализа
            
        Returns:
            Количество hops away (0 если не применимо)
        """
        hop_start = getattr(packet, 'hop_start', 0)
        hop_limit = getattr(packet, 'hop_limit', 0)
        
        if hop_start != 0 and hop_limit <= hop_start:
            hops_away = hop_start - hop_limit
            return hops_away if hops_away > 0 else 0
        
        return 0
    
    @staticmethod
    def is_admin_packet(packet: mesh_pb2.MeshPacket) -> bool:
        """
        Проверяет, является ли пакет Admin сообщением
        
        Args:
            packet: MeshPacket для проверки
            
        Returns:
            True если это Admin пакет, False иначе
        """
        payload_type = packet.WhichOneof('payload_variant')
        if payload_type == 'decoded':
            if hasattr(packet.decoded, 'portnum'):
                return packet.decoded.portnum == portnums_pb2.PortNum.ADMIN_APP
        return False


