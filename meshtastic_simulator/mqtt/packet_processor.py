"""
Обработка входящих MQTT пакетов
"""

import struct
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
from ..utils.logger import info, debug, error, warn


class MQTTPacketProcessor:
    """Обработка входящих MQTT пакетов"""
    
    def __init__(self, node_id: str, channels: Channels, node_db: Optional[NodeDB] = None):
        """
        Инициализирует процессор пакетов
        
        Args:
            node_id: Node ID для фильтрации собственных пакетов
            channels: Объект Channels для расшифровки
            node_db: Объект NodeDB для обновления информации об узлах
        """
        self.node_id = node_id
        self.channels = channels
        self.node_db = node_db
        self.MESHTASTIC_PKC_OVERHEAD = 12
    
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
                info("MQTT", f"🔍 CUSTOM TOPIC ПОЛУЧЕН: topic={topic_str}, payload_size={payload_size}")
            else:
                debug("MQTT", f"Получено MQTT сообщение: topic={topic_str}, payload_size={payload_size}")
            
            # Парсим ServiceEnvelope
            envelope = mqtt_pb2.ServiceEnvelope()
            envelope.ParseFromString(msg.payload)
            
            debug("MQTT", f"ServiceEnvelope: channel_id={envelope.channel_id}, gateway_id={envelope.gateway_id}, has_packet={envelope.HasField('packet')}")
            
            if envelope.channel_id == "Custom":
                info("MQTT", f"🔍 CUSTOM КАНАЛ ОБНАРУЖЕН: topic={topic_str}, channel_id={envelope.channel_id}, gateway_id={envelope.gateway_id}")
            
            if not envelope.packet or not envelope.channel_id:
                warn("MQTT", "Неверный ServiceEnvelope: отсутствует packet или channel_id")
                return False
            
            # Проверяем разрешения канала
            channel_allowed, ch = self.validate_channel(envelope.channel_id)
            if not channel_allowed:
                return False
            
            # Проверяем, не является ли это наш собственный пакет
            if self._is_own_packet(envelope.gateway_id):
                if envelope.channel_id == "Custom":
                    info("MQTT", f"🔍 Custom канал: игнорируем свой собственный пакет (gateway_id={envelope.gateway_id}, наш node_id={self.node_id})")
                else:
                    debug("MQTT", f"Игнорируем свой собственный пакет (gateway_id={envelope.gateway_id}, наш node_id={self.node_id})")
                return False
            
            info("MQTT", f"Получен пакет от {envelope.gateway_id} на канале {envelope.channel_id}")
            
            # Копируем пакет
            packet = mesh_pb2.MeshPacket()
            packet.CopyFrom(envelope.packet)
            
            # Копируем поля
            setattr(packet, 'from', getattr(envelope.packet, 'from', 0))
            packet.to = getattr(envelope.packet, 'to', 0)
            packet.id = getattr(envelope.packet, 'id', 0)
            packet.channel = getattr(envelope.packet, 'channel', 0)
            packet.hop_limit = getattr(envelope.packet, 'hop_limit', 0)
            packet.hop_start = getattr(envelope.packet, 'hop_start', 0)
            packet.want_ack = getattr(envelope.packet, 'want_ack', False)
            
            # Логируем информацию о трассировке маршрута
            hops_away = 0
            if packet.hop_start != 0 and packet.hop_limit <= packet.hop_start:
                hops_away = packet.hop_start - packet.hop_limit
                if hops_away > 0:
                    debug("MQTT", f"Трассировка маршрута: hops_away={hops_away}, hop_start={packet.hop_start}, hop_limit={packet.hop_limit}")
            
            # Расшифровываем пакет если нужно
            payload_type = packet.WhichOneof('payload_variant')
            if payload_type == 'encrypted':
                decrypted = self.decrypt_packet(packet, ch, envelope.channel_id)
                if not decrypted:
                    return False
                payload_type = packet.WhichOneof('payload_variant')
            
            # Устанавливаем метаданные
            if hasattr(packet, 'via_mqtt'):
                packet.via_mqtt = True
            if hasattr(packet, 'transport_mechanism'):
                try:
                    packet.transport_mechanism = mesh_pb2.MeshPacket.TransportMechanism.TRANSPORT_MQTT
                except:
                    pass
            
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
                debug("MQTT", f"🔍 Custom канал обработка: payload_type={payload_type}, original_channel={original_channel}, ch.index={ch.index if ch else 'N/A'}")
            
            # Обновляем NodeDB
            if self.node_db:
                self._update_node_db(packet, ch)
            
            # Отправляем пакет клиенту
            from_radio = mesh_pb2.FromRadio()
            from_radio.packet.CopyFrom(packet)
            
            serialized = from_radio.SerializeToString()
            framed = StreamAPI.add_framing(serialized)
            to_client_queue.put(framed)
            
            return True
        except Exception as e:
            error("MQTT", f"Ошибка обработки сообщения: {e}")
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
            debug("MQTT", f"PKI канал разрешен")
            return True, None
        
        # Ищем канал по имени
        try:
            ch = self.channels.get_by_name(channel_id)
            channel_global_id = self.channels.get_global_id(ch.index)
            
            debug("MQTT", f"Найден канал: channel_id={channel_id}, global_id={channel_global_id}, index={ch.index}, downlink_enabled={ch.settings.downlink_enabled}")
            
            if channel_id == "Custom":
                debug("MQTT", f"🔍 Custom канал проверка: channel_id={channel_id}, global_id={channel_global_id}, match={channel_id.lower() == channel_global_id.lower()}, downlink_enabled={ch.settings.downlink_enabled}")
            
            # Проверяем, что это тот же канал и downlink включен
            if channel_id.lower() == channel_global_id.lower() and ch.settings.downlink_enabled:
                if channel_id == "Custom":
                    debug("MQTT", f"✅ Custom канал разрешен для приема")
                else:
                    debug("MQTT", f"Канал '{channel_id}' разрешен для приема")
                return True, ch
            else:
                if channel_id == "Custom":
                    warn("MQTT", f"❌ Custom канал НЕ разрешен: downlink_enabled={ch.settings.downlink_enabled if ch else 'N/A'}, match={channel_id.lower() == channel_global_id.lower()}")
                else:
                    debug("MQTT", f"Пропуск пакета: канал '{channel_id}' не разрешен (downlink_enabled={ch.settings.downlink_enabled if ch else 'N/A'}, match={channel_id.lower() == channel_global_id.lower()})")
                return False, None
        except Exception as e:
            if channel_id == "Custom":
                error("MQTT", f"❌ Custom канал ошибка поиска: {e}")
            else:
                warn("MQTT", f"Ошибка поиска канала '{channel_id}': {e}")
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
                debug("PKI", f"Попытка PKI расшифровки (от !{packet_from:08X} к !{packet_to:08X})")
                warn("PKI", "PKI расшифровка пока не реализована (требуется Curve25519)")
        
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
                            info("MQTT", f"Пакет расшифрован с канала {ch_idx} (hash={channel_hash})")
                            return True
                    except Exception as e:
                        continue
                except Exception as e:
                    continue
        
        if channel_id == "Custom":
            warn("MQTT", f"❌ Custom канал: не удалось расшифровать пакет (hash={channel_hash})")
        else:
            warn("MQTT", f"Не удалось расшифровать пакет (hash={channel_hash})")
        return False
    
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
                            error("NODE", f"Ошибка обновления NodeInfo: {e}")
                    
                    elif packet.decoded.portnum == portnums_pb2.PortNum.TELEMETRY_APP:
                        try:
                            if telemetry_pb2:
                                telemetry = telemetry_pb2.Telemetry()
                                telemetry.ParseFromString(packet.decoded.payload)
                                variant = telemetry.WhichOneof('variant')
                                if variant == 'device_metrics':
                                    self.node_db.update_telemetry(packet_from, telemetry.device_metrics)
                        except Exception as e:
                            error("NODE", f"Ошибка обновления telemetry: {e}")
                    
                    elif packet.decoded.portnum == portnums_pb2.PortNum.POSITION_APP:
                        try:
                            position = mesh_pb2.Position()
                            position.ParseFromString(packet.decoded.payload)
                            self.node_db.update_position(packet_from, position)
                        except Exception as e:
                            error("NODE", f"Ошибка обновления позиции: {e}")
        except Exception as e:
            error("NODE", f"Ошибка обновления NodeDB: {e}")

