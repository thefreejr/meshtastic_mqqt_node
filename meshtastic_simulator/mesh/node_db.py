"""
База данных узлов (из firmware/src/mesh/NodeDB.cpp)
"""

from typing import Any, Optional

from meshtastic import mesh_pb2

from ..utils.logger import LogLevel, debug, error, info, log, warn
from .rtc import RTCQuality, get_valid_time


class NodeDB:
    """База данных узлов (из firmware/src/mesh/NodeDB.cpp)"""
    
    def __init__(self, our_node_num: int) -> None:
        self.our_node_num = our_node_num
        self.nodes = {}  # dict[node_num] -> NodeInfo
        self.read_index = 0
    
    def get_mesh_node(self, node_num: int) -> Optional[mesh_pb2.NodeInfo]:
        """Получает информацию о узле по номеру"""
        return self.nodes.get(node_num)
    
    def get_or_create_mesh_node(self, node_num: int) -> mesh_pb2.NodeInfo:
        """Получает или создает новую запись о узле"""
        if node_num in self.nodes:
            return self.nodes[node_num]
        
        node_info = mesh_pb2.NodeInfo()
        node_info.num = node_num & 0x7FFFFFFF
        node_info.user.id = f"!{node_num:08X}"
        self.nodes[node_num] = node_info
        info("NODE", f"Created new node entry: {node_info.user.id}")
        return node_info
    
    def update_from(self, packet: mesh_pb2.MeshPacket) -> None:
        """Обновляет информацию о узле из входящего пакета"""
        packet_from = getattr(packet, 'from', 0)
        if not packet_from or packet_from == self.our_node_num:
            return
        
        if packet.WhichOneof('payload_variant') != 'decoded':
            return
        
        node_info = self.get_or_create_mesh_node(packet_from)
        
        # Устанавливаем last_heard (как в firmware NodeDB::updateFrom)
        # Используем rx_time из пакета, если доступно, иначе используем текущее время
        if hasattr(packet, 'rx_time') and packet.rx_time:
            node_info.last_heard = packet.rx_time
        else:
            # Используем get_valid_time как в firmware (RTCQualityFromNet)
            valid_time = get_valid_time(RTCQuality.FROM_NET)
            if valid_time > 0:
                node_info.last_heard = valid_time
            else:
                # Fallback на текущее время если RTC не установлен
                import time
                node_info.last_heard = int(time.time())
        
        if hasattr(packet, 'rx_snr') and packet.rx_snr:
            node_info.snr = packet.rx_snr
        if hasattr(packet, 'via_mqtt'):
            node_info.via_mqtt = packet.via_mqtt
        # Устанавливаем hops_away (как в firmware NodeDB::updateFrom)
        # Если hopStart был установлен и limit <= hopStart, вычисляем hops_away
        if hasattr(packet, 'hop_start') and hasattr(packet, 'hop_limit'):
            if packet.hop_start != 0 and packet.hop_limit <= packet.hop_start:
                hops_away = packet.hop_start - packet.hop_limit
                node_info.hops_away = hops_away
                # В protobuf Python для optional полей флаг HasField устанавливается автоматически при установке значения
                debug("NODE", f"Set hops_away={hops_away} for node !{packet_from:08X} (hop_start={packet.hop_start}, hop_limit={packet.hop_limit})")
            else:
                # Если hops_away не может быть вычислен, устанавливаем 0 (прямой сосед)
                # Но только если поле еще не установлено
                if not hasattr(node_info, 'hops_away') or not node_info.HasField('hops_away'):
                    node_info.hops_away = 0
    
    def update_telemetry(self, node_num: int, device_metrics: Any) -> None:
        """Обновляет информацию о telemetry устройства (как в firmware NodeDB::updateTelemetry)"""
        try:
            node_info = self.get_or_create_mesh_node(node_num)
            if hasattr(node_info, 'device_metrics'):
                # ВАЖНО: Копируем телеметрию (как в firmware: info->device_metrics = t.variant.device_metrics)
                node_info.device_metrics.CopyFrom(device_metrics)
                # В protobuf Python флаг HasField('device_metrics') устанавливается автоматически при CopyFrom,
                # но только если хотя бы одно поле установлено и не равно дефолтному
                # Убеждаемся, что battery_level установлен (не 0), чтобы флаг был установлен
                if not hasattr(node_info.device_metrics, 'battery_level') or node_info.device_metrics.battery_level == 0:
                    node_info.device_metrics.battery_level = getattr(device_metrics, 'battery_level', 100)
                battery_level = getattr(node_info.device_metrics, 'battery_level', 0)
                debug("NODE", f"Updated telemetry for !{node_num:08X}: battery={battery_level}, has_field={node_info.HasField('device_metrics')}")
        except Exception as e:
            error("NODE", f"Ошибка обновления telemetry: {e}")
            import traceback
            traceback.print_exc()
    
    def update_position(self, node_num: int, position: mesh_pb2.Position):
        """Обновляет информацию о позиции узла"""
        try:
            node_info = self.get_or_create_mesh_node(node_num)
            if hasattr(node_info, 'position'):
                node_info.position.CopyFrom(position)
        except Exception as e:
            error("NODE", f"Ошибка обновления позиции: {e}")
    
    def update_user(self, node_num: int, user: mesh_pb2.User, channel: int) -> bool:
        """Обновляет информацию о пользователе узла"""
        node_info = self.get_or_create_mesh_node(node_num)
        
        if hasattr(user, 'public_key') and len(user.public_key) == 32:
            if not hasattr(node_info.user, 'public_key') or len(node_info.user.public_key) != 32:
                node_info.user.public_key = user.public_key
                debug("NODE", f"Сохранен public_key для узла !{node_num:08X}")
            elif node_info.user.public_key != user.public_key:
                warn("NODE", f"Public Key mismatch для узла !{node_num:08X}, пропускаем обновление")
                return False
        
        user.id = f"!{node_num:08X}"
        changed = (
            node_info.user.id != user.id or
            node_info.user.long_name != user.long_name or
            node_info.user.short_name != user.short_name or
            node_info.user.hw_model != user.hw_model
        )
        
        node_info.user.CopyFrom(user)
        node_info.channel = channel
        
        if changed:
            info("NODE", f"Обновлена информация: {node_info.user.id} ({user.long_name}/{user.short_name})")
        
        return changed
    
    def get_num_mesh_nodes(self) -> int:
        """Возвращает количество узлов в базе данных"""
        return len(self.nodes)
    
    def get_all_nodes(self) -> list[mesh_pb2.NodeInfo]:
        """Возвращает список всех узлов (кроме нашего)"""
        return [node for node_num, node in self.nodes.items() if node_num != self.our_node_num]

