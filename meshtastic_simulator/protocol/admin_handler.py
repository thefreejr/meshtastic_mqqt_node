"""
Обработчик AdminMessage - обработка всех типов Admin сообщений
"""

import random
from typing import Optional, Callable

try:
    from meshtastic import mesh_pb2
    from meshtastic.protobuf import admin_pb2, portnums_pb2
    try:
        from meshtastic.protobuf import telemetry_pb2
    except ImportError:
        telemetry_pb2 = None
except ImportError:
    print("Ошибка: Установите meshtastic: pip install meshtastic")
    raise

from ..config import MAX_NUM_CHANNELS
from ..mesh.config_storage import NodeConfig
from ..utils.logger import info, debug, warn, error

try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False


class AdminMessageHandler:
    """Обработчик AdminMessage - централизованная обработка всех типов Admin сообщений"""
    
    @staticmethod
    def create_reply_packet(original_packet: mesh_pb2.MeshPacket,
                            admin_response: admin_pb2.AdminMessage,
                            our_node_num: int) -> mesh_pb2.MeshPacket:
        """
        Создает reply пакет для Admin сообщения (общая логика setReplyTo из firmware)
        
        Args:
            original_packet: Исходный пакет с запросом
            admin_response: AdminMessage с ответом
            our_node_num: Наш node_num (для поля from)
            
        Returns:
            MeshPacket с ответом
        """
        reply_packet = mesh_pb2.MeshPacket()
        reply_packet.id = random.randint(1, 0xFFFFFFFF)
        
        # setReplyTo логика (из firmware MeshModule.cpp:233-245)
        packet_from = getattr(original_packet, 'from', 0)
        reply_packet.to = packet_from  # 0 означает локальный узел для TCP
        
        setattr(reply_packet, 'from', our_node_num)  # Устанавливаем from в наш node_num
        reply_packet.channel = original_packet.channel
        reply_packet.decoded.request_id = original_packet.id  # ID запроса
        reply_packet.want_ack = False  # Для TCP клиента from=0, поэтому want_ack=False
        reply_packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
        reply_packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
        reply_packet.decoded.payload = admin_response.SerializeToString()
        
        return reply_packet


