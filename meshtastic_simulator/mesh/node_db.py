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
        
        # Устанавливаем hops_away (как в firmware NodeDB::updateFrom, строка 1761-1765)
        # Если hopStart был установлен и limit <= hopStart, вычисляем hops_away
        # ВАЖНО: Соответствует firmware - устанавливаем только если условие выполняется
        if hasattr(packet, 'hop_start') and hasattr(packet, 'hop_limit'):
            if packet.hop_start != 0 and packet.hop_limit <= packet.hop_start:
                hops_away = packet.hop_start - packet.hop_limit
                node_info.hops_away = hops_away
                # В protobuf Python для optional полей флаг HasField устанавливается автоматически при установке значения
                debug("NODE", f"Set hops_away={hops_away} for node !{packet_from:08X} (hop_start={packet.hop_start}, hop_limit={packet.hop_limit})")
            # ВАЖНО: Если условие не выполняется, НЕ устанавливаем hops_away (как в firmware)
            # Это означает, что has_hops_away остается false, и клиент может показать "hops: ?"
            # Для пакетов через MQTT с hop_start = hop_limit это даст hops_away = 0, что правильно
    
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
        """Обновляет информацию о позиции узла (как в firmware NodeDB::updatePosition)"""
        try:
            node_info = self.get_or_create_mesh_node(node_num)
            # Инициализируем позицию, если её нет
            if not hasattr(node_info, 'position'):
                node_info.position = mesh_pb2.Position()
            
            # Обновляем позицию (как в firmware)
            # В Python protobuf нет поля has_position - позиция считается установленной, если есть координаты
            node_info.position.CopyFrom(position)
            
            debug("NODE", f"Updated position for node !{node_num:08X}: lat={position.latitude_i}, lon={position.longitude_i}")
        except Exception as e:
            error("NODE", f"Ошибка обновления позиции: {e}")
    
    def update_user(self, node_num: int, user: mesh_pb2.User, channel: int) -> bool:
        """Обновляет информацию о пользователе узла (как в firmware NodeDB::updateUser)"""
        node_info = self.get_or_create_mesh_node(node_num)
        
        # ВАЖНО: Логика обработки публичного ключа (как в firmware NodeDB.cpp:1670-1699)
        # Проверка публичного ключа выполняется только для удаленных узлов (не для нашего собственного)
        # (как в firmware: `if (p.public_key.size == 32 && nodeId != nodeDB->getNodeNum())`)
        is_our_node = (node_num == self.our_node_num)
        
        # ВАЖНО: Логика обработки публичного ключа (как в firmware NodeDB.cpp:1690-1699)
        # Если у узла уже есть публичный ключ, проверяем совпадение
        # Если ключ не совпадает, отклоняем обновление (как в firmware)
        existing_public_key = None
        if hasattr(node_info.user, 'public_key') and len(node_info.user.public_key) == 32:
            existing_public_key = node_info.user.public_key
        
        incoming_public_key = None
        if hasattr(user, 'public_key') and len(user.public_key) == 32:
            incoming_public_key = user.public_key
        
        # Проверяем совпадение ключей только для удаленных узлов (как в firmware)
        # Для нашего собственного узла пропускаем проверку ключа
        if not is_our_node and existing_public_key and incoming_public_key:
            # У узла уже есть ключ И в новом User есть ключ - проверяем совпадение
            if existing_public_key != incoming_public_key:
                warn("NODE", f"Public Key mismatch для узла !{node_num:08X}, пропускаем обновление (как в firmware)")
                return False
            else:
                debug("NODE", f"Public Key set for node !{node_num:08X}, not updating (как в firmware)")
        elif incoming_public_key and not existing_public_key:
            # У узла нет ключа, но в новом User есть - сохраняем новый (как в firmware: "Update Node Pubkey!")
            if not is_our_node:
                info("NODE", f"Update Node Pubkey для !{node_num:08X}!")
            else:
                debug("NODE", f"Update Node Pubkey для нашего собственного узла !{node_num:08X}")
        
        # ВАЖНО: Сохраняем существующий публичный ключ, если он есть и в новом User его нет
        # (как в firmware - ключ не теряется при обновлении других полей)
        saved_public_key = existing_public_key if existing_public_key else (incoming_public_key if incoming_public_key else None)
        
        user.id = f"!{node_num:08X}"
        changed = (
            node_info.user.id != user.id or
            node_info.user.long_name != user.long_name or
            node_info.user.short_name != user.short_name or
            node_info.user.hw_model != user.hw_model
        )
        
        # Копируем User (как в firmware: info->user = lite)
        # ВАЖНО: Если в новом user нет публичного ключа, но у узла он есть, сохраняем его перед CopyFrom
        if saved_public_key and not incoming_public_key:
            # Временно устанавливаем ключ в user перед CopyFrom, чтобы он не потерялся
            user.public_key = saved_public_key
            debug("NODE", f"Временно установлен существующий public_key в user перед CopyFrom для узла !{node_num:08X}")
        
        node_info.user.CopyFrom(user)
        
        # ВАЖНО: Убеждаемся, что публичный ключ сохранен после CopyFrom
        # (на случай, если CopyFrom все равно перезаписал его)
        if saved_public_key:
            if not hasattr(node_info.user, 'public_key') or len(node_info.user.public_key) != 32 or node_info.user.public_key != saved_public_key:
                node_info.user.public_key = saved_public_key
                debug("NODE", f"Восстановлен public_key для узла !{node_num:08X} после CopyFrom")
            else:
                debug("NODE", f"Public_key сохранен для узла !{node_num:08X} (длина={len(node_info.user.public_key)})")
        
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

