"""
Обработка входящих MQTT пакетов
"""

import random
import struct
import time
import queue
from typing import Optional, Tuple, Any

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    print("Ошибка: Установите cryptography: pip install cryptography")
    raise

try:
    from meshtastic import mesh_pb2, mqtt_pb2
    from meshtastic.protobuf import portnums_pb2
    try:
        from meshtastic.protobuf import telemetry_pb2
    except ImportError:
        telemetry_pb2 = None
except ImportError:
    print("Ошибка: Установите meshtastic: pip install meshtastic")
    raise

from ..mesh.channels import Channels
from ..mesh.node_db import NodeDB
from ..mesh.rtc import RTCQuality, get_valid_time
from ..protocol.stream_api import StreamAPI
from ..protocol.packet_handler import PacketHandler
from ..utils.logger import info, debug, error, warn


class MQTTPacketProcessor:
    """Обработка входящих MQTT пакетов"""
    
    def __init__(self, node_id: str, channels: Channels, node_db: Optional[NodeDB] = None, server = None):
        """
        Инициализирует процессор пакетов
        
        Args:
            node_id: Node ID для фильтрации собственных пакетов
            channels: Объект Channels для расшифровки
            node_db: Объект NodeDB для обновления информации об узлах
            server: Ссылка на TCPServer для доступа к сессиям (для получения информации о пользователе)
        """
        self.node_id = node_id
        self.channels = channels
        self.node_db = node_db
        self.server = server  # Ссылка на TCPServer
        self.MESHTASTIC_PKC_OVERHEAD = 12
        # Отслеживаем, когда мы последний раз отправляли NodeInfo каждому узлу (чтобы не отправлять слишком часто)
        self.last_nodeinfo_sent = {}  # {node_num: timestamp}
    
    def process_mqtt_message(self, msg: Any, to_client_queue: Any) -> bool:
        """
        Обрабатывает входящее MQTT сообщение
        
        Args:
            msg: MQTT сообщение (объект с полями topic и payload)
            to_client_queue: Очередь для отправки обработанных пакетов клиенту
            
        Returns:
            True если пакет успешно обработан, False иначе
        """
        try:
            # Логируем входящее сообщение
            topic_str = msg.topic if hasattr(msg, 'topic') else str(msg.topic)
            payload_size = len(msg.payload) if hasattr(msg, 'payload') else 0
            
            if "Custom" in topic_str:
                info("MQTT", f"🔍 CUSTOM TOPIC RECEIVED: topic={topic_str}, payload_size={payload_size}")
            else:
                debug("MQTT", f"Received MQTT message: topic={topic_str}, payload_size={payload_size}")
            
            # Парсим ServiceEnvelope
            envelope = mqtt_pb2.ServiceEnvelope()
            try:
                envelope.ParseFromString(msg.payload)
            except Exception as e:
                error("MQTT", f"Error parsing ServiceEnvelope: {e} (topic: {topic_str}, payload_size: {payload_size})")
                return False
            
            debug("MQTT", f"ServiceEnvelope: channel_id={envelope.channel_id}, gateway_id={envelope.gateway_id}, has_packet={envelope.HasField('packet')}")
            
            if envelope.channel_id == "Custom":
                info("MQTT", f"🔍 CUSTOM CHANNEL DETECTED: topic={topic_str}, channel_id={envelope.channel_id}, gateway_id={envelope.gateway_id}")
            
            if not envelope.packet or not envelope.channel_id:
                warn("MQTT", "Invalid ServiceEnvelope: missing packet or channel_id")
                return False
            
            # Проверяем разрешения канала
            channel_allowed, ch = self.validate_channel(envelope.channel_id)
            if not channel_allowed:
                return False
            
            # Получаем поле from и to из envelope перед проверкой
            envelope_from = getattr(envelope.packet, 'from', 0) if envelope.packet else 0
            envelope_to = getattr(envelope.packet, 'to', 0) if envelope.packet else 0
            our_node_num = int(self.node_id[1:], 16) if self.node_id.startswith('!') else int(self.node_id, 16)
            our_node_num = our_node_num & 0x7FFFFFFF
            
            # Проверяем, не является ли это наш собственный пакет
            # gateway_id - это node_id отправителя пакета в MQTT (того, кто опубликовал)
            # Если gateway_id == наш node_id, это пакет, который мы сами отправили
            # НО: если packet.to указывает на нас (не broadcast), это пакет для нас от другого узла - обрабатываем!
            # ВАЖНО: ACK пакеты (ROUTING_APP с request_id) всегда обрабатываем, даже если gateway_id совпадает,
            # так как это ответ на наш запрос (как в firmware - ACK обрабатываются даже если от собственного gateway)
            is_own_gateway = self._is_own_packet(envelope.gateway_id)
            is_for_us = (envelope_to != 0xFFFFFFFF and envelope_to == our_node_num)
            
            # Проверяем, является ли это ACK пакетом (ROUTING_APP с request_id)
            is_ack_packet = False
            if envelope.packet and hasattr(envelope.packet, 'decoded'):
                if (hasattr(envelope.packet.decoded, 'portnum') and 
                    envelope.packet.decoded.portnum == portnums_pb2.PortNum.ROUTING_APP and
                    hasattr(envelope.packet.decoded, 'request_id') and 
                    envelope.packet.decoded.request_id != 0):
                    is_ack_packet = True
            
            # ВАЖНО: Если это наш собственный пакет (gateway_id совпадает) и он от нас (isFromUs),
            # отправляем implicit ACK локально клиенту (как в firmware MQTT.cpp:66-70)
            # Это нужно для того, чтобы клиент знал, что пакет был доставлен хотя бы одному узлу
            if is_own_gateway and envelope.packet:
                # Проверяем, является ли пакет от нас (packet.from == наш node_num)
                packet_from_envelope = getattr(envelope.packet, 'from', 0)
                is_from_us = (packet_from_envelope == our_node_num)
                
                if is_from_us:
                    # Это наш собственный пакет - отправляем implicit ACK локально клиенту
                    # (как в firmware: "Generate an implicit ACK towards ourselves (handled and processed only locally!)")
                    try:
                        # Находим сессию отправителя для отправки локального ACK
                        sender_node_id = self.node_id if isinstance(self.node_id, str) else f"!{self.node_id:08X}"
                        sender_session = None
                        if self.server:
                            with self.server.sessions_lock:
                                for session in self.server.active_sessions.values():
                                    if session.node_id == sender_node_id:
                                        sender_session = session
                                        break
                        
                        if sender_session and envelope.packet.want_ack:
                            # Проверяем, нужно ли отправлять ACK с want_ack=true для надежной доставки
                            # (для собственных пакетов это обычно False, так как isFromUs)
                            ack_wants_ack = PacketHandler.should_success_ack_with_want_ack(envelope.packet, our_node_num)
                            
                            # Создаем локальный ACK для клиента (не через MQTT, а напрямую клиенту)
                            ack_packet = PacketHandler.create_ack_packet(
                                envelope.packet,
                                our_node_num,
                                envelope.packet.channel if envelope.packet.channel < 8 else 0,
                                error_reason=None,
                                ack_wants_ack=ack_wants_ack
                            )
                            
                            # Отправляем ACK напрямую клиенту (локально, не через MQTT)
                            from_radio_ack = mesh_pb2.FromRadio()
                            from_radio_ack.packet.CopyFrom(ack_packet)
                            serialized_ack = from_radio_ack.SerializeToString()
                            framed_ack = StreamAPI.add_framing(serialized_ack)
                            # Используем put_nowait для избежания блокировки
                            try:
                                to_client_queue.put_nowait(framed_ack)
                            except queue.Full:
                                warn("MQTT", f"Client queue is full, dropping ACK packet")
                            debug("ACK", f"Sent implicit ACK locally for own packet {envelope.packet.id} (gateway_id={envelope.gateway_id}, request_id={ack_packet.decoded.request_id})")
                        else:
                            # Пакет не требует ACK (want_ack=False) - это нормально для POSITION_APP и других пакетов
                            if sender_session:
                                debug("MQTT", f"Own packet {envelope.packet.id} does not require ACK (want_ack=False), skipping implicit ACK")
                    except Exception as e:
                        debug("ACK", f"Error sending implicit ACK for own packet: {e}")
                    
                    # Игнорируем собственный пакет (не обрабатываем дальше)
                    # ВАЖНО: Для пакетов с want_ack=False это нормально - они не требуют ACK
                    if envelope.packet.want_ack:
                        debug("MQTT", f"Ignoring own packet (gateway_id={envelope.gateway_id}, packet.from={packet_from_envelope:08X}, implicit ACK sent locally)")
                    else:
                        debug("MQTT", f"Ignoring own packet (gateway_id={envelope.gateway_id}, packet.from={packet_from_envelope:08X}, no ACK needed)")
                    return False
                else:
                    # Это не наш пакет, но gateway_id совпадает - игнорируем
                    debug("MQTT", f"Ignoring downlink message we originally sent (gateway_id={envelope.gateway_id}, packet.from={packet_from_envelope:08X})")
                    return False
            
            # Игнорируем только если это наш собственный пакет И он не адресован нам напрямую И это не ACK пакет
            # (если packet.to указывает на нас, это ответ от другого узла - обрабатываем)
            if is_own_gateway and not is_for_us and not is_ack_packet:
                if envelope.channel_id == "Custom":
                    info("MQTT", f"🔍 Custom channel: ignoring own packet (gateway_id={envelope.gateway_id}, our node_id={self.node_id})")
                else:
                    debug("MQTT", f"Ignoring own packet (gateway_id={envelope.gateway_id}, our node_id={self.node_id}, packet.from={envelope_from:08X}, packet.to={envelope_to:08X})")
                return False
            
            # Логируем ACK пакеты для отладки
            if is_ack_packet:
                debug("ACK", f"Received ACK packet: gateway_id={envelope.gateway_id}, packet.from={envelope_from:08X}, packet.to={envelope_to:08X}, request_id={envelope.packet.decoded.request_id if hasattr(envelope.packet.decoded, 'request_id') else 'N/A'}")
            
            # Логируем получение пакета
            info("MQTT", f"Received packet from {envelope.gateway_id} on channel {envelope.channel_id}, packet.from={envelope_from:08X}, packet.to={envelope_to:08X}, our_node={our_node_num:08X}")
            
            # Копируем пакет
            packet = mesh_pb2.MeshPacket()
            packet.CopyFrom(envelope.packet)
            
            # Копируем поля
            setattr(packet, 'from', envelope_from)
            packet.to = getattr(envelope.packet, 'to', 0)
            packet.id = getattr(envelope.packet, 'id', 0)
            packet.channel = getattr(envelope.packet, 'channel', 0)
            packet.hop_limit = getattr(envelope.packet, 'hop_limit', 0)
            packet.hop_start = getattr(envelope.packet, 'hop_start', 0)
            packet.want_ack = getattr(envelope.packet, 'want_ack', False)
            
            # Проверяем, что поле from установлено правильно
            packet_from = getattr(packet, 'from', 0)
            if packet_from != envelope_from:
                warn("MQTT", f"Packet.from mismatch: envelope={envelope_from:08X}, packet={packet_from:08X}")
            
            # Логируем информацию о трассировке маршрута
            hops_away = 0
            if packet.hop_start != 0 and packet.hop_limit <= packet.hop_start:
                hops_away = packet.hop_start - packet.hop_limit
                if hops_away > 0:
                    debug("MQTT", f"Route trace: hops_away={hops_away}, hop_start={packet.hop_start}, hop_limit={packet.hop_limit}")
            
            # Расшифровываем пакет если нужно
            payload_type = packet.WhichOneof('payload_variant')
            is_pki_channel = envelope.channel_id == "PKI"
            if payload_type == 'encrypted':
                decrypted = self.decrypt_packet(packet, ch, envelope.channel_id)
                # ВАЖНО: Для PKI канала пакеты принимаются даже если не расшифрованы
                # (как в firmware MQTT.cpp:117-123 - PKI messages get accepted even if we can't decrypt)
                if not decrypted and not is_pki_channel:
                    return False
                # Для PKI канала проверяем условия приема (как в firmware)
                if not decrypted and is_pki_channel:
                    packet_from = getattr(packet, 'from', 0)
                    packet_to = packet.to
                    is_broadcast = packet_to == 0xFFFFFFFF or packet_to == 0xFFFFFFFE
                    is_to_us = packet_to == our_node_num if not is_broadcast else False
                    
                    # ВАЖНО: Проверяем наличие публичного ключа у отправителя (как в firmware ReliableRouter.cpp:122-126)
                    # Если у отправителя нет публичного ключа, отправляем NodeInfo с публичным ключом
                    from_node = self.node_db.get_mesh_node(packet_from) if self.node_db else None
                    from_has_public_key = (from_node and hasattr(from_node.user, 'public_key') and 
                                          len(from_node.user.public_key) == 32)
                    
                    if not from_has_public_key and is_to_us and packet.want_ack:
                        # У отправителя нет публичного ключа, и пакет адресован нам с want_ack
                        # Отправляем NodeInfo с публичным ключом (как в firmware: "PKI packet from unknown node, send PKI_UNKNOWN_PUBKEY")
                        info("PKI", f"PKI packet from !{packet_from:08X} without public key, sending NodeInfo with public key (as in firmware)")
                        # Находим сессию получателя для отправки NodeInfo
                        receiver_node_id = self.node_id if isinstance(self.node_id, str) else f"!{self.node_id:08X}"
                        receiver_session = None
                        if self.server:
                            with self.server.sessions_lock:
                                for session in self.server.active_sessions.values():
                                    if session.node_id == receiver_node_id:
                                        receiver_session = session
                                        break
                        if receiver_session:
                            self._send_receiver_nodeinfo_to_sender(receiver_session, packet_from, packet.channel)
                    
                    # Принимаем PKI сообщения если:
                    # 1. Адресованы нам (isToUs)
                    # 2. ИЛИ у нас есть информация об отправителе и получателе в NodeDB (tx && tx->has_user && rx && rx->has_user)
                    if is_to_us:
                        info("PKI", f"Accepting PKI message to us (from !{packet_from:08X} to !{packet_to:08X}) even though not decrypted")
                    elif self.node_db:
                        to_node = self.node_db.get_mesh_node(packet_to) if not is_broadcast else None
                        from_has_user = (from_node and hasattr(from_node, 'user') and 
                                        ((hasattr(from_node.user, 'short_name') and from_node.user.short_name) or
                                         (hasattr(from_node.user, 'long_name') and from_node.user.long_name)))
                        to_has_user = (to_node and hasattr(to_node, 'user') and 
                                      ((hasattr(to_node.user, 'short_name') and to_node.user.short_name) or
                                       (hasattr(to_node.user, 'long_name') and to_node.user.long_name)))
                        if from_has_user and (is_broadcast or to_has_user):
                            info("PKI", f"Accepting PKI message (from !{packet_from:08X} to !{packet_to:08X}) even though not decrypted - both nodes have user info")
                        else:
                            debug("PKI", f"Rejecting PKI message (from !{packet_from:08X} to !{packet_to:08X}) - missing user info (from_has_user={from_has_user}, to_has_user={to_has_user if not is_broadcast else 'N/A'})")
                            return False
                    else:
                        debug("PKI", f"Rejecting PKI message (from !{packet_from:08X} to !{packet_to:08X}) - not to us and no NodeDB")
                    return False
                payload_type = packet.WhichOneof('payload_variant')
            
            # Устанавливаем метаданные
            if hasattr(packet, 'via_mqtt'):
                packet.via_mqtt = True
            if hasattr(packet, 'transport_mechanism'):
                try:
                    packet.transport_mechanism = mesh_pb2.MeshPacket.TransportMechanism.TRANSPORT_MQTT
                except Exception as e:
                    debug("MQTT", f"Error setting transport_mechanism: {e}")
            
            # Устанавливаем rx_time
            if hasattr(packet, 'rx_time'):
                rx_time = get_valid_time(RTCQuality.FROM_NET)
                if rx_time > 0:
                    packet.rx_time = rx_time
            
            # Устанавливаем правильный канал
            original_channel = packet.channel
            if payload_type == 'decoded':
                if ch and packet.channel != ch.index:
                    packet.channel = ch.index
            elif payload_type == 'encrypted':
                pass  # Channel для encrypted остается как hash
            
            if envelope.channel_id == "Custom":
                debug("MQTT", f"🔍 Custom channel processing: payload_type={payload_type}, original_channel={original_channel}, ch.index={ch.index if ch else 'N/A'}")
            
            # Обновляем NodeDB
            packet_from = getattr(packet, 'from', 0)
            
            # ВАЖНО: Как в firmware MeshService::handleFromRadio - проверяем ДО обновления NodeDB,
            # есть ли у получателя информация о пользователе отправителя
            # Если нет - отправляем NodeInfo получателя отправителю (как в firmware nodeInfoModule->sendOurNodeInfo)
            should_send_our_nodeinfo = False
            if packet_from and self.server and self.node_db:
                try:
                    # Проверяем условия для отправки NodeInfo (как в firmware)
                    should_send_nodeinfo = True
                    
                    # Пропускаем TELEMETRY_APP пакеты с request_id (как в firmware)
                    # ВАЖНО: Также пропускаем POSITION_APP пакеты - они не требуют отправки NodeInfo
                    # (как в firmware - позиция обновляется, но NodeInfo отправляется только при первом пакете от новой ноды)
                    if packet.WhichOneof('payload_variant') == 'decoded':
                        # Убеждаемся, что portnums_pb2 доступен
                        if portnums_pb2 and hasattr(packet.decoded, 'portnum'):
                            if packet.decoded.portnum == portnums_pb2.PortNum.TELEMETRY_APP:
                                if hasattr(packet.decoded, 'request_id') and packet.decoded.request_id > 0:
                                    should_send_nodeinfo = False
                                    debug("MQTT", f"Skipping NodeInfo send: telemetry response packet")
                            elif packet.decoded.portnum == portnums_pb2.PortNum.POSITION_APP:
                                # ВАЖНО: Пропускаем отправку NodeInfo для POSITION_APP пакетов
                                # (позиция обновляется, но NodeInfo отправляется только при первом пакете от новой ноды)
                                # Это предотвращает переполнение очереди клиента при частых обновлениях позиции
                                should_send_nodeinfo = False
                                debug("MQTT", f"Skipping NodeInfo send: POSITION_APP packet (position updates don't require NodeInfo)")
                    
                    if should_send_nodeinfo:
                        # Проверяем ДО обновления NodeDB, есть ли у получателя информация о пользователе отправителя
                        # (как в firmware !nodeDB->getMeshNode(mp->from)->has_user)
                        sender_node = self.node_db.get_or_create_mesh_node(packet_from)
                        sender_has_user = (hasattr(sender_node, 'user') and 
                                          ((hasattr(sender_node.user, 'short_name') and sender_node.user.short_name) or
                                           (hasattr(sender_node.user, 'long_name') and sender_node.user.long_name)))
                        
                        if not sender_has_user:
                            # Находим сессию получателя (наша сессия)
                            receiver_node_id = self.node_id if isinstance(self.node_id, str) else f"!{self.node_id:08X}"
                            receiver_session = None
                            with self.server.sessions_lock:
                                for session in self.server.active_sessions.values():
                                    if session.node_id == receiver_node_id:
                                        receiver_session = session
                                        break
                            
                            if receiver_session and receiver_session.mqtt_client and receiver_session.mqtt_client.connected:
                                should_send_our_nodeinfo = True
                                info("MQTT", f"[{receiver_session._log_prefix()}] Heard new node !{packet_from:08X}, will send our NodeInfo (as in firmware)")
                except Exception as e:
                    debug("MQTT", f"Error checking if should send NodeInfo to sender: {e}")
            
            if self.node_db:
                self._update_node_db(packet, ch)
                # ВАЖНО: Отправляем NodeInfo клиенту при получении пакета из MQTT
                # (как в firmware - клиент использует NodeInfo для отображения имени)
                # Создаем или получаем NodeInfo для отправителя
                # ВАЖНО: Если это TELEMETRY_APP пакет, телеметрия уже обновлена в NodeDB через _update_node_db
                if packet_from:
                    node_info = self.node_db.get_or_create_mesh_node(packet_from)
                    
                    # ВАЖНО: Убеждаемся, что телеметрия из NodeDB копируется в NodeInfo
                    # (NodeDB может иметь обновленную телеметрию из TELEMETRY_APP пакетов)
                    try:
                        from meshtastic.protobuf import telemetry_pb2
                        if telemetry_pb2:
                            # Проверяем, есть ли телеметрия в NodeDB
                            if hasattr(node_info, 'device_metrics') and node_info.HasField('device_metrics'):
                                # Телеметрия уже есть в node_info из NodeDB
                                debug("MQTT", f"NodeInfo for !{packet_from:08X} already has device_metrics from NodeDB")
                            else:
                                # Телеметрии нет - создаем базовую (будет обновлена из сессии ниже)
                                device_metrics = telemetry_pb2.DeviceMetrics()
                                device_metrics.battery_level = 100
                                device_metrics.voltage = 4.2
                                device_metrics.channel_utilization = 0.0
                                device_metrics.air_util_tx = 0.0
                                device_metrics.uptime_seconds = 0
                                node_info.device_metrics.CopyFrom(device_metrics)
                                debug("MQTT", f"Created default device_metrics for !{packet_from:08X} (will be updated from session)")
                    except Exception as e:
                        debug("MQTT", f"Error checking device_metrics from NodeDB: {e}")
                    
                    # Пытаемся получить информацию о пользователе и телеметрию из сессии отправителя (через gateway_id)
                    session_found = False
                    if self.server and envelope.gateway_id:
                        try:
                            # Ищем сессию по node_id (gateway_id) или по packet_from (node_num)
                            # Нормализуем gateway_id (приводим к верхнему регистру для сравнения)
                            gateway_node_id = envelope.gateway_id if isinstance(envelope.gateway_id, str) else f"!{envelope.gateway_id:08X}"
                            gateway_node_id = gateway_node_id.upper()  # Приводим к верхнему регистру
                            packet_from_node_id = f"!{packet_from:08X}"  # node_id из packet.from
                            debug("MQTT", f"Looking for session with gateway_id={gateway_node_id}, packet_from={packet_from_node_id}")
                            with self.server.sessions_lock:
                                for session in self.server.active_sessions.values():
                                    session_node_id = session.node_id.upper() if isinstance(session.node_id, str) else session.node_id
                                    session_node_num = session.node_num & 0x7FFFFFFF
                                    debug("MQTT", f"Checking session: node_id={session.node_id}, node_num={session_node_num:08X}, owner={session.owner.short_name if hasattr(session, 'owner') and session.owner.short_name else 'N/A'}")
                                    # Ищем по gateway_id (node_id сессии) или по packet_from (node_num сессии)
                                    if session_node_id == gateway_node_id or session_node_num == packet_from:
                                        session_found = True
                                        # Нашли сессию отправителя - используем информацию о владельце
                                        node_info.user.id = session.owner.id
                                        node_info.user.long_name = session.owner.long_name
                                        node_info.user.short_name = session.owner.short_name
                                        node_info.user.is_licensed = session.owner.is_licensed
                                        # ВАЖНО: Всегда включаем публичный ключ из сессии, если он есть (для PKI)
                                        if session.owner.public_key and len(session.owner.public_key) > 0:
                                            if not session.owner.is_licensed:
                                                node_info.user.public_key = session.owner.public_key
                                        # ВАЖНО: Если в NodeDB получателя уже есть публичный ключ для этого узла, сохраняем его
                                        # (на случай, если сессия не имеет ключа, но мы его уже знаем)
                                        existing_node = self.node_db.get_mesh_node(packet_from)
                                        if existing_node and hasattr(existing_node.user, 'public_key') and len(existing_node.user.public_key) == 32:
                                            if not hasattr(node_info.user, 'public_key') or len(node_info.user.public_key) != 32:
                                                node_info.user.public_key = existing_node.user.public_key
                                                debug("MQTT", f"Восстановлен public_key из NodeDB для !{packet_from:08X} при отправке NodeInfo клиенту")
                                        
                                        # Добавляем телеметрию из сессии отправителя (если есть)
                                        try:
                                            from meshtastic.protobuf import telemetry_pb2
                                            if telemetry_pb2:
                                                # Получаем телеметрию из NodeDB сессии отправителя
                                                sender_node = session.node_db.get_mesh_node(session.node_num)
                                                if sender_node and hasattr(sender_node, 'device_metrics') and sender_node.HasField('device_metrics'):
                                                    # Копируем телеметрию, но обновляем uptime_seconds
                                                    node_info.device_metrics.CopyFrom(sender_node.device_metrics)
                                                    node_info.device_metrics.uptime_seconds = session.get_uptime_seconds()
                                                    # Убеждаемся, что хотя бы одно поле установлено (для protobuf)
                                                    if not hasattr(node_info.device_metrics, 'battery_level') or node_info.device_metrics.battery_level == 0:
                                                        node_info.device_metrics.battery_level = 100
                                                    debug("MQTT", f"Added device_metrics from session for !{packet_from:08X} (uptime={node_info.device_metrics.uptime_seconds}, battery={node_info.device_metrics.battery_level})")
                                                else:
                                                    # Создаем базовую телеметрию с обязательными полями
                                                    device_metrics = telemetry_pb2.DeviceMetrics()
                                                    device_metrics.battery_level = 100
                                                    device_metrics.voltage = 4.2
                                                    device_metrics.channel_utilization = 0.0
                                                    device_metrics.air_util_tx = 0.0
                                                    device_metrics.uptime_seconds = session.get_uptime_seconds()
                                                    node_info.device_metrics.CopyFrom(device_metrics)
                                                    debug("MQTT", f"Created default device_metrics for !{packet_from:08X} (uptime={device_metrics.uptime_seconds}, battery={device_metrics.battery_level})")
                                        except Exception as e:
                                            debug("MQTT", f"Error adding device_metrics: {e}")
                                            import traceback
                                            traceback.print_exc()
                                        
                                        # Обновляем в NodeDB
                                        self.node_db.update_user(packet_from, node_info.user, ch.index if ch else 0)
                                        
                                        # Обновляем телеметрию в NodeDB получателя
                                        if hasattr(node_info, 'device_metrics') and node_info.HasField('device_metrics'):
                                            self.node_db.update_telemetry(packet_from, node_info.device_metrics)
                                        
                                        debug("MQTT", f"Got user info from session: !{packet_from:08X} ({node_info.user.short_name})")
                                        break
                            
                            if not session_found:
                                # Это нормально для внешних узлов (не локальных сессий)
                                # Используем информацию из NodeDB, которая обновляется при получении пакетов
                                debug("MQTT", f"Session not found for gateway_id={gateway_node_id}, packet_from=!{packet_from:08X} (external node, using NodeDB)")
                                # Если сессия не найдена, пытаемся получить информацию из NodeDB
                                # (может быть, это пакет от другого узла, который уже обработан ранее)
                                if hasattr(node_info, 'user') and node_info.user.short_name:
                                    debug("MQTT", f"Using existing user info from NodeDB: !{packet_from:08X} ({node_info.user.short_name})")
                                
                                # Даже если сессия не найдена, добавляем телеметрию из NodeDB получателя, если она есть
                                try:
                                    from meshtastic.protobuf import telemetry_pb2
                                    if telemetry_pb2:
                                        if hasattr(node_info, 'device_metrics') and node_info.HasField('device_metrics'):
                                            # Телеметрия уже есть в NodeDB
                                            debug("MQTT", f"Using existing device_metrics from NodeDB for !{packet_from:08X}")
                                        else:
                                            # Создаем базовую телеметрию
                                            device_metrics = telemetry_pb2.DeviceMetrics()
                                            device_metrics.battery_level = 100
                                            device_metrics.voltage = 4.2
                                            device_metrics.channel_utilization = 0.0
                                            device_metrics.air_util_tx = 0.0
                                            device_metrics.uptime_seconds = 0
                                            node_info.device_metrics.CopyFrom(device_metrics)
                                            debug("MQTT", f"Created default device_metrics for !{packet_from:08X} (no session found)")
                                except Exception as e:
                                    debug("MQTT", f"Error adding device_metrics when session not found: {e}")
                        except Exception as e:
                            debug("MQTT", f"Error getting user info from session: {e}")
                            import traceback
                            traceback.print_exc()
                    
                    # ВАЖНО: Убеждаемся, что телеметрия всегда есть в NodeInfo перед отправкой клиенту
                    # (как в firmware TypeConversions::ConvertToNodeInfo: if (lite->has_device_metrics) { info.has_device_metrics = true; info.device_metrics = lite->device_metrics; })
                    try:
                        from meshtastic.protobuf import telemetry_pb2
                        if telemetry_pb2:
                            # Проверяем, есть ли телеметрия в NodeInfo (из NodeDB или из сессии)
                            has_telemetry = hasattr(node_info, 'device_metrics') and node_info.HasField('device_metrics')
                            
                            if not has_telemetry:
                                # Телеметрия не установлена - создаем базовую
                                device_metrics = telemetry_pb2.DeviceMetrics()
                                device_metrics.battery_level = 100
                                device_metrics.voltage = 4.2
                                device_metrics.channel_utilization = 0.0
                                device_metrics.air_util_tx = 0.0
                                device_metrics.uptime_seconds = 0
                                node_info.device_metrics.CopyFrom(device_metrics)
                                debug("MQTT", f"Added default device_metrics to NodeInfo for !{packet_from:08X} before sending to client")
                            
                            # ВАЖНО: В protobuf Python флаг HasField('device_metrics') устанавливается автоматически при CopyFrom,
                            # НО только если хотя бы одно поле установлено и не равно дефолтному значению
                            # Убеждаемся, что battery_level и voltage установлены (не равны 0/0.0)
                            if hasattr(node_info, 'device_metrics'):
                                # Проверяем, установлен ли флаг после CopyFrom
                                if not node_info.HasField('device_metrics'):
                                    # Флаг не установлен - устанавливаем обязательные поля
                                    if not hasattr(node_info.device_metrics, 'battery_level') or node_info.device_metrics.battery_level == 0:
                                        node_info.device_metrics.battery_level = 100
                                    if not hasattr(node_info.device_metrics, 'voltage') or node_info.device_metrics.voltage == 0.0:
                                        node_info.device_metrics.voltage = 4.2
                                    debug("MQTT", f"Fixed device_metrics fields for !{packet_from:08X} to ensure HasField is set")
                                
                                # Финальная проверка
                                if node_info.HasField('device_metrics'):
                                    battery = getattr(node_info.device_metrics, 'battery_level', 0)
                                    voltage = getattr(node_info.device_metrics, 'voltage', 0.0)
                                    uptime = getattr(node_info.device_metrics, 'uptime_seconds', 0)
                                    debug("MQTT", f"NodeInfo for !{packet_from:08X} has device_metrics: battery={battery}, voltage={voltage}, uptime={uptime}, HasField=True")
                                else:
                                    warn("MQTT", f"NodeInfo for !{packet_from:08X} HasField('device_metrics') is False after all fixes!")
                            
                            # ВАЖНО: Убеждаемся, что hops_away установлен (как в firmware TypeConversions::ConvertToNodeInfo)
                            # Если hops_away не установлен, устанавливаем 0 (прямой сосед через MQTT)
                            if not hasattr(node_info, 'hops_away') or not node_info.HasField('hops_away'):
                                node_info.hops_away = 0
                                debug("MQTT", f"Set hops_away=0 for node !{packet_from:08X} (direct neighbor via MQTT)")
                            else:
                                debug("MQTT", f"NodeInfo for !{packet_from:08X} has hops_away={node_info.hops_away}")
                    except Exception as e:
                        debug("MQTT", f"Error ensuring device_metrics in NodeInfo: {e}")
                        import traceback
                        traceback.print_exc()
                    
                    # Отправляем NodeInfo клиенту, если есть информация о пользователе
                    # ВАЖНО: Пропускаем отправку NodeInfo для POSITION_APP пакетов (как в firmware)
                    # Это предотвращает переполнение очереди клиента при частых обновлениях позиции
                    is_position_packet = False
                    if packet.WhichOneof('payload_variant') == 'decoded':
                        if portnums_pb2 and hasattr(packet.decoded, 'portnum'):
                            is_position_packet = (packet.decoded.portnum == portnums_pb2.PortNum.POSITION_APP)
                    
                    has_user_info = (hasattr(node_info, 'user') and 
                                    ((hasattr(node_info.user, 'short_name') and node_info.user.short_name) or
                                     (hasattr(node_info.user, 'long_name') and node_info.user.long_name)))
                    
                    # ВАЖНО: Не отправляем NodeInfo для POSITION_APP пакетов (позиция обновляется, но NodeInfo не нужен)
                    if has_user_info and not is_position_packet:
                        # Проверяем, что телеметрия установлена перед отправкой
                        has_telemetry = hasattr(node_info, 'device_metrics') and node_info.HasField('device_metrics')
                        if not has_telemetry:
                            warn("MQTT", f"NodeInfo for !{packet_from:08X} has no device_metrics before sending to client!")
                        
                        # ВАЖНО: Создаем новый NodeInfo для FromRadio (как в firmware: fromRadioScratch.node_info = infoToSend)
                        # В firmware используется прямое присваивание, а не CopyFrom
                        from_radio_node_info = mesh_pb2.FromRadio()
                        from_radio_node_info.node_info.CopyFrom(node_info)
                        
                        # ВАЖНО: В protobuf Python CopyFrom может не копировать вложенные сообщения правильно,
                        # если флаг HasField не установлен. Убеждаемся, что телеметрия скопировалась
                        if node_info.HasField('device_metrics'):
                            # Копируем телеметрию явно (как в firmware: info.device_metrics = lite->device_metrics)
                            if not from_radio_node_info.node_info.HasField('device_metrics'):
                                from_radio_node_info.node_info.device_metrics.CopyFrom(node_info.device_metrics)
                                debug("MQTT", f"Manually copied device_metrics to FromRadio.node_info for !{packet_from:08X}")
                            else:
                                # Проверяем, что значения совпадают
                                if (getattr(from_radio_node_info.node_info.device_metrics, 'battery_level', 0) != 
                                    getattr(node_info.device_metrics, 'battery_level', 0)):
                                    from_radio_node_info.node_info.device_metrics.CopyFrom(node_info.device_metrics)
                                    debug("MQTT", f"Updated device_metrics in FromRadio.node_info for !{packet_from:08X} (values didn't match)")
                        else:
                            warn("MQTT", f"NodeInfo for !{packet_from:08X} has no device_metrics before CopyFrom to FromRadio!")
                        
                        # Дополнительная проверка после копирования
                        has_telemetry_after = hasattr(from_radio_node_info.node_info, 'device_metrics') and from_radio_node_info.node_info.HasField('device_metrics')
                        if has_telemetry != has_telemetry_after:
                            warn("MQTT", f"NodeInfo device_metrics lost during CopyFrom: before={has_telemetry}, after={has_telemetry_after}")
                        
                        # Логируем детали телеметрии перед отправкой
                        if has_telemetry_after:
                            battery = from_radio_node_info.node_info.device_metrics.battery_level if hasattr(from_radio_node_info.node_info.device_metrics, 'battery_level') else 'N/A'
                            voltage = from_radio_node_info.node_info.device_metrics.voltage if hasattr(from_radio_node_info.node_info.device_metrics, 'voltage') else 'N/A'
                            uptime = from_radio_node_info.node_info.device_metrics.uptime_seconds if hasattr(from_radio_node_info.node_info.device_metrics, 'uptime_seconds') else 'N/A'
                            debug("MQTT", f"NodeInfo device_metrics before send: battery={battery}, voltage={voltage}, uptime={uptime}")
                        
                        # ВАЖНО: Проверяем, что телеметрия есть в сериализованном FromRadio
                        # (как в firmware: fromRadioScratch.node_info = infoToSend)
                        serialized_node_info = from_radio_node_info.SerializeToString()
                        
                        # Проверяем, что телеметрия включена в сериализованные данные
                        # Десериализуем обратно для проверки
                        try:
                            test_from_radio = mesh_pb2.FromRadio()
                            test_from_radio.ParseFromString(serialized_node_info)
                            if test_from_radio.WhichOneof('payload_variant') == 'node_info':
                                test_has_telemetry = test_from_radio.node_info.HasField('device_metrics')
                                if test_has_telemetry:
                                    test_battery = getattr(test_from_radio.node_info.device_metrics, 'battery_level', 0)
                                    test_voltage = getattr(test_from_radio.node_info.device_metrics, 'voltage', 0.0)
                                    debug("MQTT", f"✅ Serialized FromRadio.node_info has device_metrics: battery={test_battery}, voltage={test_voltage}")
                                else:
                                    warn("MQTT", f"❌ Serialized FromRadio.node_info has NO device_metrics after serialization!")
                        except Exception as e:
                            debug("MQTT", f"Error checking serialized FromRadio: {e}")
                        
                        framed_node_info = StreamAPI.add_framing(serialized_node_info)
                        # Используем put_nowait для избежания блокировки
                        try:
                            to_client_queue.put_nowait(framed_node_info)
                        except queue.Full:
                            warn("MQTT", f"Client queue is full, dropping NodeInfo packet")
                        short_name = node_info.user.short_name if hasattr(node_info.user, 'short_name') and node_info.user.short_name else 'N/A'
                        debug("MQTT", f"Sent NodeInfo to client for node !{packet_from:08X} ({short_name}, telemetry={has_telemetry_after}, battery={battery if has_telemetry_after else 'N/A'})")
                        
                        # ВАЖНО: В firmware телеметрия также отправляется отдельным пакетом TELEMETRY_APP клиенту
                        # (как в DeviceTelemetryModule::sendTelemetry с phoneOnly=true)
                        # Отправляем телеметрию отдельным пакетом, если она есть
                        if has_telemetry_after and node_info.HasField('device_metrics'):
                            try:
                                # portnums_pb2 уже импортирован на уровне модуля, импортируем только telemetry_pb2
                                from meshtastic.protobuf import telemetry_pb2
                                if telemetry_pb2 and portnums_pb2:
                                    # Создаем Telemetry пакет (как в firmware DeviceTelemetryModule)
                                    telemetry = telemetry_pb2.Telemetry()
                                    telemetry.time = int(time.time())
                                    telemetry.device_metrics.CopyFrom(node_info.device_metrics)
                                    
                                    # Создаем MeshPacket с Telemetry payload (portnum=TELEMETRY_APP)
                                    telemetry_packet = mesh_pb2.MeshPacket()
                                    telemetry_packet.id = random.randint(1, 0xFFFFFFFF)
                                    telemetry_packet.to = 0  # To phone (0 = local)
                                    setattr(telemetry_packet, 'from', packet_from)
                                    telemetry_packet.channel = 0
                                    telemetry_packet.decoded.portnum = portnums_pb2.PortNum.TELEMETRY_APP
                                    telemetry_packet.decoded.payload = telemetry.SerializeToString()
                                    telemetry_packet.want_ack = False
                                    
                                    # Создаем FromRadio с MeshPacket
                                    from_radio_telemetry = mesh_pb2.FromRadio()
                                    from_radio_telemetry.packet.CopyFrom(telemetry_packet)
                                    
                                    # Отправляем клиенту
                                    serialized_telemetry = from_radio_telemetry.SerializeToString()
                                    framed_telemetry = StreamAPI.add_framing(serialized_telemetry)
                                    # Используем put_nowait для избежания блокировки
                                    try:
                                        to_client_queue.put_nowait(framed_telemetry)
                                    except queue.Full:
                                        warn("MQTT", f"Client queue is full, dropping Telemetry packet")
                                    debug("MQTT", f"Sent TELEMETRY_APP packet to client for node !{packet_from:08X} (battery={node_info.device_metrics.battery_level})")
                            except Exception as e:
                                debug("MQTT", f"Error sending TELEMETRY_APP packet: {e}")
                                import traceback
                                traceback.print_exc()
                    
                    # Отправляем NodeInfo получателя отправителю, если нужно (проверка была сделана ДО обновления NodeDB)
                    # ВАЖНО: Отправляем даже если информация о пользователе уже есть, но была обновлена только что
                    # (как в firmware - отправляем NodeInfo при первом пакете от новой ноды)
                    if should_send_our_nodeinfo:
                        try:
                            receiver_node_id = self.node_id if isinstance(self.node_id, str) else f"!{self.node_id:08X}"
                            receiver_session = None
                            with self.server.sessions_lock:
                                for session in self.server.active_sessions.values():
                                    if session.node_id == receiver_node_id:
                                        receiver_session = session
                                        break
                            
                            if receiver_session and receiver_session.mqtt_client and receiver_session.mqtt_client.connected:
                                # Отправляем NodeInfo получателя отправителю через MQTT
                                self._send_receiver_nodeinfo_to_sender(receiver_session, packet_from, packet.channel)
                        except Exception as e:
                            debug("MQTT", f"Error sending our NodeInfo to sender: {e}")
                    
                    # ВАЖНО: Также отправляем NodeInfo получателя отправителю при broadcast пакетах
                    # (как в firmware - при получении broadcast пакета отправляем NodeInfo, чтобы отправитель знал о получателе)
                    if packet_from and self.server and self.node_db:
                        try:
                            packet_to = getattr(packet, 'to', 0)
                            is_broadcast = (packet_to == 0xFFFFFFFF)
                            
                            # Отправляем NodeInfo получателя отправителю при broadcast пакетах
                            # (это нужно, чтобы отправитель знал о получателе, даже если информация о пользователе отправителя уже есть)
                            # ВАЖНО: Отправляем даже если should_send_our_nodeinfo уже был установлен ранее,
                            # но только если прошло достаточно времени (контролируется в _send_receiver_nodeinfo_to_sender)
                            if is_broadcast:
                                receiver_node_id = self.node_id if isinstance(self.node_id, str) else f"!{self.node_id:08X}"
                                receiver_session = None
                                with self.server.sessions_lock:
                                    for session in self.server.active_sessions.values():
                                        if session.node_id == receiver_node_id:
                                            receiver_session = session
                                            break
                                
                                if receiver_session and receiver_session.mqtt_client and receiver_session.mqtt_client.connected:
                                    # Отправляем NodeInfo получателя отправителю при broadcast пакетах
                                    # (как в firmware - при получении broadcast пакета отправляем NodeInfo)
                                    info("MQTT", f"[{receiver_session._log_prefix()}] Received broadcast from !{packet_from:08X}, sending our NodeInfo (as in firmware)")
                                    self._send_receiver_nodeinfo_to_sender(receiver_session, packet_from, packet.channel)
                        except Exception as e:
                            debug("MQTT", f"Error sending our NodeInfo after broadcast: {e}")
            
            # Отправляем пакет клиенту (как в firmware MeshService::sendToPhone)
            # В firmware все пакеты отправляются клиенту, независимо от того, от кого они пришли
            # ВАЖНО: Для PKI канала пакеты отправляются даже если не расшифрованы (как в firmware MQTT.cpp:117-123)
            # ВАЖНО: Проверяем, что очередь не переполнена (предотвращает утечку памяти)
            # ВАЖНО: Для собственных POSITION_APP пакетов из MQTT не отправляем пакет клиенту
            # (клиент уже отправил этот пакет, не нужно отправлять его обратно)
            is_own_position_packet = False
            if is_own_gateway and packet.WhichOneof('payload_variant') == 'decoded':
                if portnums_pb2 and hasattr(packet.decoded, 'portnum'):
                    is_own_position_packet = (packet.decoded.portnum == portnums_pb2.PortNum.POSITION_APP)
            
            if not is_own_position_packet:
                try:
                    from_radio = mesh_pb2.FromRadio()
                    from_radio.packet.CopyFrom(packet)
                    
                    serialized = from_radio.SerializeToString()
                    framed = StreamAPI.add_framing(serialized)
                    # Используем put_nowait для избежания блокировки, если очередь переполнена
                    try:
                        to_client_queue.put_nowait(framed)
                    except queue.Full:
                        # Если очередь переполнена, логируем предупреждение и пропускаем пакет
                        # (лучше потерять пакет, чем накапливать их в памяти)
                        warn("MQTT", f"Client queue is full, dropping packet from !{packet_from:08X}")
                except Exception as e:
                    error("MQTT", f"Error preparing packet for client: {e}")
            else:
                # Собственный POSITION_APP пакет из MQTT - не отправляем клиенту (клиент уже отправил его)
                debug("MQTT", f"Skipping sending own POSITION_APP packet to client (packet.id={packet.id}, from=!{packet_from:08X})")
            
            # Логируем отправку пакета клиенту
            payload_type = packet.WhichOneof('payload_variant')
            portnum_info = 'N/A'
            if payload_type == 'decoded' and hasattr(packet.decoded, 'portnum'):
                portnum_info = packet.decoded.portnum
            elif payload_type == 'encrypted':
                portnum_info = 'encrypted'
            
            if is_pki_channel:
                info("PKI", f"Sent PKI packet to client: from=!{packet_from:08X}, to=!{envelope_to:08X}, payload_type={payload_type}, portnum={portnum_info}")
            else:
                debug("MQTT", f"Sent packet to client: from=!{packet_from:08X}, to=!{envelope_to:08X}, payload_type={payload_type}, portnum={portnum_info}")
            
            # ВАЖНО: Отправляем ACK/NAK для пакетов с want_ack=true, полученных через MQTT
            # (как в firmware ReliableRouter::sniffReceived - проверяет want_ack и отправляет ACK/NAK)
            # ВАЖНО: ACK отправляется не только для decoded пакетов, но и для encrypted пакетов,
            # которые адресованы нам, но не могут быть расшифрованы (с ошибкой NO_CHANNEL или PKI_UNKNOWN_PUBKEY)
            packet_to = packet.to
            is_broadcast = (packet_to == 0xFFFFFFFF or packet_to == 0xFFFFFFFE)
            is_to_us = (packet_to == our_node_num) if not is_broadcast else False
            
            if packet.want_ack and is_to_us and not is_broadcast:
                # Проверяем, что это не ACK сам по себе (Routing сообщение с request_id)
                is_routing_ack = False
                if payload_type == 'decoded':
                    is_routing_ack = (
                        hasattr(packet.decoded, 'portnum') and 
                        packet.decoded.portnum == portnums_pb2.PortNum.ROUTING_APP and
                        hasattr(packet.decoded, 'request_id') and 
                        packet.decoded.request_id != 0
                    )
                
                # Отправляем ACK только если:
                # 1. Пакет адресован нам (не broadcast)
                # 2. Это не ACK сам по себе
                # 3. Для decoded пакетов - это не Admin пакет (Admin пакеты обрабатываются отдельно)
                is_admin = False
                if payload_type == 'decoded':
                    is_admin = (hasattr(packet.decoded, 'portnum') and 
                               packet.decoded.portnum == portnums_pb2.PortNum.ADMIN_APP)
                
                if not is_routing_ack and not is_admin:
                    try:
                        # Определяем тип ошибки для NAK (как в firmware ReliableRouter::sniffReceived)
                        # Если пакет не расшифрован и адресован нам, отправляем NAK с соответствующей ошибкой
                        ack_error = None  # По умолчанию ACK (NONE)
                        
                        if payload_type == 'encrypted':
                            # Пакет не расшифрован - отправляем NAK с ошибкой
                            # Проверяем, является ли это PKI пакетом (channel == 0)
                            if packet.channel == 0:
                                # PKI пакет - проверяем наличие публичного ключа
                                from_node = self.node_db.get_mesh_node(packet_from) if self.node_db else None
                                if not from_node or not hasattr(from_node.user, 'public_key') or len(from_node.user.public_key) != 32:
                                    ack_error = mesh_pb2.Routing.Error.PKI_UNKNOWN_PUBKEY
                                    info("ACK", f"PKI packet from !{packet_from:08X} without public key, sending PKI_UNKNOWN_PUBKEY NAK")
                                else:
                                    # PKI пакет, но не расшифрован - возможно, проблема с ключом получателя
                                    ack_error = mesh_pb2.Routing.Error.NO_CHANNEL
                                    debug("ACK", f"PKI packet from !{packet_from:08X} not decrypted, sending NO_CHANNEL NAK")
                            else:
                                # Обычный зашифрованный пакет - нет канала для расшифровки
                                ack_error = mesh_pb2.Routing.Error.NO_CHANNEL
                                debug("ACK", f"Encrypted packet from !{packet_from:08X} not decrypted, sending NO_CHANNEL NAK")
                        
                        # Проверяем, нужно ли отправлять ACK с want_ack=true для надежной доставки
                        # (как в firmware shouldSuccessAckWithWantAck)
                        ack_wants_ack = False
                        if ack_error is None:  # Только для ACK (не NAK)
                            ack_wants_ack = PacketHandler.should_success_ack_with_want_ack(packet, our_node_num)
                        
                        # Создаем ACK/NAK пакет (как в firmware MeshModule::allocAckNak и RoutingModule::sendAckNak)
                        ack_packet = PacketHandler.create_ack_packet(
                            packet, 
                            our_node_num, 
                            packet.channel if packet.channel < 8 else 0,
                            ack_error,  # Передаем ошибку для NAK (None = ACK)
                            ack_wants_ack=ack_wants_ack
                        )
                        
                        # Находим сессию получателя (наша сессия) для отправки ACK через MQTT
                        receiver_node_id = self.node_id if isinstance(self.node_id, str) else f"!{self.node_id:08X}"
                        receiver_session = None
                        if self.server:
                            with self.server.sessions_lock:
                                for session in self.server.active_sessions.values():
                                    if session.node_id == receiver_node_id:
                                        receiver_session = session
                                        break
                        
                        if receiver_session and receiver_session.mqtt_client and receiver_session.mqtt_client.connected:
                            channel_index = packet.channel if packet.channel < 8 else 0
                            
                            # ВАЖНО: Детальное логирование для диагностики статуса доставки
                            # Android клиент устанавливает статус "доставка подтверждена" только если:
                            # fromId == p?.data?.to, где fromId = packet.from из ACK, p?.data?.to = to исходного пакета
                            ack_from = getattr(ack_packet, 'from', 0)
                            ack_to = ack_packet.to
                            want_ack_info = f", want_ack={ack_wants_ack}" if ack_wants_ack else ""
                            
                            # Проверяем, правильно ли установлены поля для статуса "доставка подтверждена"
                            # Для прямых сообщений: ack_from (получатель) должен быть равен packet_to (получатель)
                            will_be_received = (ack_error is None and ack_from == packet_to)
                            status_info = " (will be RECEIVED)" if will_be_received else " (will be DELIVERED)"
                            
                            if ack_error is None:
                                info("ACK", f"✅ Sent ACK via MQTT: packet_id={packet.id}, request_id={ack_packet.decoded.request_id}, ack_from=!{ack_from:08X}, ack_to=!{ack_to:08X}, original_from=!{packet_from:08X}, original_to=!{packet_to:08X}{status_info}{want_ack_info}")
                            else:
                                info("ACK", f"❌ Sent NAK via MQTT: packet_id={packet.id}, request_id={ack_packet.decoded.request_id}, error={ack_error}, ack_from=!{ack_from:08X}, ack_to=!{ack_to:08X}, original_from=!{packet_from:08X}, original_to=!{packet_to:08X}")
                            
                            receiver_session.mqtt_client.publish_packet(ack_packet, channel_index)
                        else:
                            debug("ACK", f"Could not send ACK via MQTT: receiver_session not found or MQTT not connected")
                    except Exception as e:
                        error("ACK", f"Error sending ACK via MQTT: {e}")
                        import traceback
                        traceback.print_exc()
            
            return True
        except Exception as e:
            error("MQTT", f"Error processing message: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def validate_channel(self, channel_id: str) -> Tuple[bool, Optional[Any]]:
        """
        Проверяет разрешения канала
        
        Args:
            channel_id: ID канала из ServiceEnvelope
            
        Returns:
            Tuple (channel_allowed: bool, channel_object или None)
        """
        # Обработка PKI канала
        if channel_id == "PKI":
            debug("MQTT", f"PKI channel allowed")
            return True, None
        
        # Ищем канал по имени
        try:
            ch = self.channels.get_by_name(channel_id)
            channel_global_id = self.channels.get_global_id(ch.index)
            
            debug("MQTT", f"Found channel: channel_id={channel_id}, global_id={channel_global_id}, index={ch.index}, downlink_enabled={ch.settings.downlink_enabled}")
            
            if channel_id == "Custom":
                debug("MQTT", f"🔍 Custom channel check: channel_id={channel_id}, global_id={channel_global_id}, match={channel_id.lower() == channel_global_id.lower()}, downlink_enabled={ch.settings.downlink_enabled}")
            
            # Проверяем, что это тот же канал и downlink включен
            if channel_id.lower() == channel_global_id.lower() and ch.settings.downlink_enabled:
                if channel_id == "Custom":
                    debug("MQTT", f"✅ Custom channel allowed for receive")
                else:
                    debug("MQTT", f"Channel '{channel_id}' allowed for receive")
                return True, ch
            else:
                if channel_id == "Custom":
                    warn("MQTT", f"Custom channel NOT allowed: downlink_enabled={ch.settings.downlink_enabled if ch else 'N/A'}, match={channel_id.lower() == channel_global_id.lower()}")
                else:
                    debug("MQTT", f"Skipping packet: channel '{channel_id}' not allowed (downlink_enabled={ch.settings.downlink_enabled if ch else 'N/A'}, match={channel_id.lower() == channel_global_id.lower()})")
                return False, None
        except Exception as e:
            if channel_id == "Custom":
                error("MQTT", f"Custom channel search error: {e}")
            else:
                warn("MQTT", f"Error searching for channel '{channel_id}': {e}")
            import traceback
            traceback.print_exc()
            return False, None
    
    def decrypt_packet(self, packet: mesh_pb2.MeshPacket, ch: Optional[Any], channel_id: str) -> bool:
        """
        Расшифровывает зашифрованный пакет
        
        Args:
            packet: MeshPacket с encrypted payload
            ch: Объект канала (может быть None для PKI)
            channel_id: ID канала для логирования
            
        Returns:
            True если расшифровка успешна, False иначе
        """
        encrypted_data = packet.encrypted if hasattr(packet, 'encrypted') else b''
        
        if not encrypted_data:
            return False
        
        encrypted_size = len(encrypted_data)
        packet_from = getattr(packet, 'from', 0)
        packet_to = packet.to
        
        is_broadcast = packet_to == 0xFFFFFFFF or packet_to == 0xFFFFFFFE
        is_to_us = packet_to == self.node_db.our_node_num if self.node_db else False
        
        # Попытка PKI расшифровки (если применимо)
        if (packet.channel == 0 and is_to_us and packet_to > 0 and not is_broadcast and
            encrypted_size > self.MESHTASTIC_PKC_OVERHEAD and self.node_db):
            from_node = self.node_db.get_mesh_node(packet_from)
            to_node = self.node_db.get_mesh_node(packet_to)
            
            if (from_node and to_node and 
                hasattr(from_node.user, 'public_key') and len(from_node.user.public_key) == 32 and
                hasattr(to_node.user, 'public_key') and len(to_node.user.public_key) == 32):
                debug("PKI", f"Attempting PKI decryption (from !{packet_from:08X} to !{packet_to:08X})")
                warn("PKI", "PKI decryption not yet implemented (requires Curve25519)")
        
        # Попытка расшифровки через каналы
        channel_hash = packet.channel
        for ch_idx in range(len(self.channels.channels)):
            if self.channels.decrypt_for_hash(ch_idx, channel_hash):
                try:
                    key = self.channels._get_key(ch_idx)
                    if key is None:
                        continue
                    
                    nonce = bytearray(16)
                    packet_id = packet.id
                    from_node = getattr(packet, 'from', 0)
                    struct.pack_into('<Q', nonce, 0, packet_id)
                    struct.pack_into('<I', nonce, 8, from_node)
                    
                    backend = default_backend()
                    cipher = Cipher(algorithms.AES(key), modes.CTR(bytes(nonce)), backend=backend)
                    decryptor = cipher.decryptor()
                    decrypted_data = decryptor.update(encrypted_data) + decryptor.finalize()
                    
                    try:
                        data = mesh_pb2.Data()
                        data.ParseFromString(decrypted_data)
                        
                        if data.portnum != portnums_pb2.PortNum.UNKNOWN_APP:
                            packet.decoded.CopyFrom(data)
                            packet.channel = ch_idx
                            packet.ClearField('encrypted')
                            info("MQTT", f"Packet decrypted from channel {ch_idx} (hash={channel_hash})")
                            return True
                    except Exception as e:
                        continue
                except Exception as e:
                    continue
        
        if channel_id == "Custom":
            warn("MQTT", f"Custom channel: failed to decrypt packet (hash={channel_hash})")
        else:
            warn("MQTT", f"Failed to decrypt packet (hash={channel_hash})")
        return False
    
    def _send_receiver_nodeinfo_to_sender(self, receiver_session, sender_node_num: int, channel: int) -> None:
        """Отправляет NodeInfo получателя отправителю через MQTT (как в firmware NodeInfoModule::sendOurNodeInfo)"""
        try:
            import random
            import time
            from ..config import DEFAULT_HOP_LIMIT
            # portnums_pb2 уже импортирован на уровне модуля
            
            # Проверяем, не отправляли ли мы NodeInfo этому узлу недавно (чтобы не спамить)
            # В firmware это контролируется через throttling, но мы делаем простую проверку
            # Уменьшаем интервал до 10 секунд, чтобы ноды видели друг друга при отправке сообщений
            current_time = time.time()
            last_sent = self.last_nodeinfo_sent.get(sender_node_num, 0)
            if current_time - last_sent < 10:  # Не отправляем чаще чем раз в 10 секунд (было 60)
                debug("MQTT", f"[{receiver_session._log_prefix()}] Skipping NodeInfo send to !{sender_node_num:08X} (sent {current_time - last_sent:.1f}s ago)")
                return
            
            # Создаем User пакет с информацией о владельце получателя
            user = mesh_pb2.User()
            user.id = receiver_session.owner.id
            user.long_name = receiver_session.owner.long_name
            user.short_name = receiver_session.owner.short_name
            user.is_licensed = receiver_session.owner.is_licensed
            
            # ВАЖНО: Публичный ключ (если не лицензирован)
            # Всегда включаем публичный ключ из сессии, если он есть
            if receiver_session.owner.public_key and len(receiver_session.owner.public_key) > 0:
                if not receiver_session.owner.is_licensed:
                    user.public_key = receiver_session.owner.public_key
            # ВАЖНО: Также проверяем NodeDB получателя - возможно, там есть ключ, который нужно включить
            # (на случай, если сессия не имеет ключа, но мы его уже знаем)
            elif self.node_db:
                receiver_node = self.node_db.get_mesh_node(receiver_session.node_num)
                if receiver_node and hasattr(receiver_node.user, 'public_key') and len(receiver_node.user.public_key) == 32:
                    if not receiver_session.owner.is_licensed:
                        user.public_key = receiver_node.user.public_key
                        debug("MQTT", f"Включен public_key из NodeDB получателя для NodeInfo к !{sender_node_num:08X}")
            
            # Создаем MeshPacket с User payload (portnum=NODEINFO_APP)
            packet = mesh_pb2.MeshPacket()
            packet.id = random.randint(1, 0xFFFFFFFF)
            packet.to = sender_node_num  # Отправляем конкретному отправителю
            setattr(packet, 'from', receiver_session.node_num)
            packet.channel = channel if channel < 8 else 0
            packet.decoded.portnum = portnums_pb2.PortNum.NODEINFO_APP
            packet.decoded.payload = user.SerializeToString()
            packet.hop_limit = DEFAULT_HOP_LIMIT
            packet.hop_start = DEFAULT_HOP_LIMIT
            packet.want_ack = False
            
            # Отправляем в MQTT
            # ВАЖНО: gateway_id будет равен node_id получателя, но пакет должен быть получен сессией отправителя
            channel_index = packet.channel if packet.channel < 8 else 0
            receiver_session.mqtt_client.publish_packet(packet, channel_index)
            
            # Обновляем время последней отправки
            self.last_nodeinfo_sent[sender_node_num] = current_time
            
            info("MQTT", f"[{receiver_session._log_prefix()}] Sent our NodeInfo to sender !{sender_node_num:08X} (packet.from={receiver_session.node_num:08X}, packet.to={sender_node_num:08X}, as in firmware)")
        except Exception as e:
            debug("MQTT", f"Error sending receiver NodeInfo to sender: {e}")
            import traceback
            traceback.print_exc()
    
    def _is_own_packet(self, gateway_id: Any) -> bool:
        """Проверяет, является ли пакет нашим собственным"""
        gateway_id_str = gateway_id if isinstance(gateway_id, str) else f"!{gateway_id:08X}" if isinstance(gateway_id, int) else str(gateway_id)
        our_node_id_str = self.node_id if isinstance(self.node_id, str) else f"!{self.node_id:08X}" if isinstance(self.node_id, int) else str(self.node_id)
        
        gateway_id_normalized = gateway_id_str.replace('!', '').upper()
        our_node_id_normalized = our_node_id_str.replace('!', '').upper()
        
        return gateway_id_normalized == our_node_id_normalized
    
    def _update_node_db(self, packet: mesh_pb2.MeshPacket, ch: Optional[Any]) -> None:
        """Обновляет NodeDB на основе пакета"""
        if not self.node_db:
            return
        
        try:
            self.node_db.update_from(packet)
            
            if packet.WhichOneof('payload_variant') == 'decoded' and hasattr(packet.decoded, 'portnum'):
                packet_from = getattr(packet, 'from', 0)
                if packet_from:
                    if packet.decoded.portnum == portnums_pb2.PortNum.NODEINFO_APP:
                        try:
                            user = mesh_pb2.User()
                            user.ParseFromString(packet.decoded.payload)
                            self.node_db.update_user(packet_from, user, ch.index if ch else 0)
                        except Exception as e:
                            error("NODE", f"Error updating NodeInfo: {e}")
                    
                    elif packet.decoded.portnum == portnums_pb2.PortNum.TELEMETRY_APP:
                        try:
                            if telemetry_pb2:
                                telemetry = telemetry_pb2.Telemetry()
                                telemetry.ParseFromString(packet.decoded.payload)
                                variant = telemetry.WhichOneof('variant')
                                if variant == 'device_metrics':
                                    self.node_db.update_telemetry(packet_from, telemetry.device_metrics)
                        except Exception as e:
                            error("NODE", f"Error updating telemetry: {e}")
                    
                    elif packet.decoded.portnum == portnums_pb2.PortNum.POSITION_APP:
                        try:
                            position = mesh_pb2.Position()
                            position.ParseFromString(packet.decoded.payload)
                            self.node_db.update_position(packet_from, position)
                        except Exception as e:
                            error("NODE", f"Error updating position: {e}")
        except Exception as e:
            error("NODE", f"Error updating NodeDB: {e}")

