"""
TCP сервер для подключения meshtastic python CLI через StreamAPI
"""

import queue
import random
import socket
import struct
import threading
import time
import json
import os
from datetime import datetime
from pathlib import Path

try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False

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

from ..config import MAX_NUM_CHANNELS, START1, START2, HEADER_LEN, MAX_TO_FROM_RADIO_SIZE, NodeConfig, DEFAULT_HOP_LIMIT, HOP_MAX, DEFAULT_MQTT_ADDRESS, DEFAULT_MQTT_USERNAME, DEFAULT_MQTT_PASSWORD, DEFAULT_MQTT_ROOT
from ..protocol.stream_api import StreamAPI
from ..mqtt.client import MQTTClient
from ..mesh.channels import Channels
from ..mesh.node_db import NodeDB
from ..mesh.rtc import RTCQuality, get_valid_time
from ..tcp.session import TCPConnectionSession
from ..utils.logger import log, debug, info, warn, error, LogLevel


class TCPServer:
    """TCP сервер для подключения meshtastic python CLI через StreamAPI (мультисессионная архитектура)"""
    
    def __init__(self, port: int, 
                 default_mqtt_broker: str = DEFAULT_MQTT_ADDRESS,
                 default_mqtt_port: int = 1883,
                 default_mqtt_username: str = DEFAULT_MQTT_USERNAME,
                 default_mqtt_password: str = DEFAULT_MQTT_PASSWORD,
                 default_mqtt_root: str = DEFAULT_MQTT_ROOT):
        """
        Инициализирует TCP сервер для множественных подключений
        
        Args:
            port: TCP порт для прослушивания
            default_mqtt_broker: Дефолтный MQTT брокер для новых сессий
            default_mqtt_port: Дефолтный MQTT порт
            default_mqtt_username: Дефолтный MQTT username
            default_mqtt_password: Дефолтный MQTT password
            default_mqtt_root: Дефолтный MQTT root topic
        """
        self.port = port
        self.server_socket = None
        self.running = False
        
        # Дефолтные настройки MQTT для новых сессий
        self.default_mqtt_broker = default_mqtt_broker
        self.default_mqtt_port = default_mqtt_port
        self.default_mqtt_username = default_mqtt_username
        self.default_mqtt_password = default_mqtt_password
        self.default_mqtt_root = default_mqtt_root
        
        # Активные сессии: dict[client_address] -> TCPConnectionSession
        self.active_sessions = {}
        self.sessions_lock = threading.Lock()
        
        # Маппинг device_id -> node_id для сохранения настроек между переподключениями
        # device_id уникален для каждого устройства и не меняется
        self.device_id_to_node_id = {}  # dict[device_id_hex: str] -> node_id: str
        self.device_id_lock = threading.Lock()
        
        # Маппинг IP -> node_id для временной идентификации до получения device_id
        self.ip_to_node_id = {}  # dict[ip: str] -> node_id: str
        
        # Файл для сохранения маппинга device_id -> node_id
        self.device_id_mapping_file = Path("device_id_mapping.json")
        self._load_device_id_mapping()
    
    def _load_device_id_mapping(self):
        """Загружает маппинг device_id -> node_id из файла"""
        try:
            if self.device_id_mapping_file.exists():
                with open(self.device_id_mapping_file, 'r', encoding='utf-8') as f:
                    self.device_id_to_node_id = json.load(f)
                info("TCP", f"Загружено {len(self.device_id_to_node_id)} маппингов device_id -> node_id")
            else:
                debug("TCP", "Файл маппинга device_id не найден, используем пустой маппинг")
        except Exception as e:
            warn("TCP", f"Ошибка загрузки маппинга device_id: {e}")
            self.device_id_to_node_id = {}
    
    def _save_device_id_mapping(self):
        """Сохраняет маппинг device_id -> node_id в файл"""
        try:
            with open(self.device_id_mapping_file, 'w', encoding='utf-8') as f:
                json.dump(self.device_id_to_node_id, f, indent=2)
            debug("TCP", f"Сохранено {len(self.device_id_to_node_id)} маппингов device_id -> node_id")
        except Exception as e:
            warn("TCP", f"Ошибка сохранения маппинга device_id: {e}")
    
    def start(self):
        """Запускает TCP сервер"""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(('0.0.0.0', self.port))
        self.server_socket.listen(5)
        self.running = True
        
        info("TCP", f"Сервер запущен на порту {self.port}")
        
        while self.running:
            try:
                client_socket, client_address = self.server_socket.accept()
                info("TCP", f"Подключение от {client_address[0]}:{client_address[1]}")
                
                # Генерируем или получаем node_id для этого клиента
                # Сначала проверяем IP -> node_id маппинг (временная идентификация)
                # Затем, после получения device_id из MyInfo, обновим маппинг device_id -> node_id
                import hashlib
                
                client_ip = client_address[0]
                
                # Проверяем, есть ли уже node_id для этого IP
                node_id = None
                if client_ip in self.ip_to_node_id:
                    node_id = self.ip_to_node_id[client_ip]
                    info("TCP", f"Найден node_id для IP {client_ip}: {node_id}")
                else:
                    # Генерируем новый node_id на основе IP
                    client_hash = hashlib.md5(client_ip.encode()).hexdigest()
                    node_num = int(client_hash[:8], 16) & 0x7FFFFFFF
                    node_id = f"!{node_num:08X}"
                    
                    # Проверяем уникальность node_id среди активных сессий
                    with self.sessions_lock:
                        existing_node_ids = {s.node_id for s in self.active_sessions.values()}
                        if node_id in existing_node_ids:
                            # Если коллизия - добавляем смещение на основе порта
                            port_offset = client_address[1] % 1000
                            node_num = (node_num + port_offset) & 0x7FFFFFFF
                            node_id = f"!{node_num:08X}"
                            # Проверяем еще раз
                            if node_id in existing_node_ids:
                                import random
                                node_num = (node_num + random.randint(1, 1000)) & 0x7FFFFFFF
                                node_id = f"!{node_num:08X}"
                    
                    # Сохраняем временный маппинг IP -> node_id
                    self.ip_to_node_id[client_ip] = node_id
                    info("TCP", f"Сгенерирован node_id для клиента {client_address[0]}:{client_address[1]}: {node_id}")
                
                # Создаем новую сессию для этого клиента
                session = TCPConnectionSession(
                    client_socket=client_socket,
                    client_address=client_address,
                    node_id=node_id,
                    server=self  # Передаем ссылку на сервер для обновления маппинга
                )
                
                # Добавляем сессию в активные
                with self.sessions_lock:
                    self.active_sessions[client_address] = session
                
                # ВАЖНО: Сначала загружаем сохраненные настройки (включая MQTT конфигурацию),
                # затем создаем MQTT клиент с правильными настройками
                session._load_settings()
                
                # Получаем или создаем MQTT клиент для этой сессии (после загрузки настроек)
                session.get_or_create_mqtt_client(
                    default_broker=self.default_mqtt_broker,
                    default_port=self.default_mqtt_port,
                    default_username=self.default_mqtt_username,
                    default_password=self.default_mqtt_password,
                    default_root=self.default_mqtt_root
                )
                
                # Запускаем обработку клиента в отдельном потоке
                thread = threading.Thread(
                    target=self._handle_client_session,
                    args=(session,),
                    daemon=True
                )
                thread.start()
            except Exception as e:
                if self.running:
                    error("TCP", f"Ошибка приема подключения: {e}")
    
    def stop(self):
        """Останавливает TCP сервер"""
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass
        
        # Закрываем все активные сессии
        with self.sessions_lock:
            for session in list(self.active_sessions.values()):
                session.close()
            self.active_sessions.clear()
        
        info("TCP", "Сервер остановлен")
    
    def _handle_client_session(self, session: TCPConnectionSession):
        """Обрабатывает подключение TCP клиента через сессию"""
        rx_buffer = bytes()
        session.client_socket.settimeout(0.1)
        
        try:
            while self.running:
                try:
                    # Обрабатываем пакеты из MQTT для этой сессии
                    if session.mqtt_client:
                        while not session.mqtt_client.to_client_queue.empty():
                            response = session.mqtt_client.to_client_queue.get_nowait()
                            
                            try:
                                from_radio_data = StreamAPI.remove_framing(response)
                                if from_radio_data:
                                    from_radio = mesh_pb2.FromRadio()
                                    from_radio.ParseFromString(from_radio_data)
                                    if from_radio.HasField('packet'):
                                        session._handle_mqtt_packet(from_radio.packet)
                            except:
                                pass
                            
                            session.client_socket.send(response)
                except queue.Empty:
                    pass
                except Exception as e:
                    error("TCP", f"[{session._log_prefix()}] Ошибка отправки пакета клиенту: {e}")
                
                try:
                    data = session.client_socket.recv(4096)
                    if not data:
                        break
                    
                    rx_buffer += data
                    
                    while len(rx_buffer) >= HEADER_LEN:
                        if rx_buffer[0] != START1 or rx_buffer[1] != START2:
                            rx_buffer = rx_buffer[1:]
                            continue
                        
                        length = struct.unpack('>H', rx_buffer[2:4])[0]
                        if length > MAX_TO_FROM_RADIO_SIZE:
                            rx_buffer = rx_buffer[1:]
                            continue
                        
                        if len(rx_buffer) < HEADER_LEN + length:
                            break
                        
                        payload = rx_buffer[HEADER_LEN:HEADER_LEN + length]
                        rx_buffer = rx_buffer[HEADER_LEN + length:]
                        
                        session._handle_to_radio(payload)
                
                except socket.timeout:
                    continue
                except Exception as e:
                    error("TCP", f"[{session._log_prefix()}] Ошибка чтения от клиента: {e}")
                    break
        
        except Exception as e:
            error("TCP", f"[{session._log_prefix()}] Ошибка обработки клиента: {e}")
        finally:
            session.client_socket.close()
            
            # Удаляем сессию из активных
            with self.sessions_lock:
                if session.client_address in self.active_sessions:
                    del self.active_sessions[session.client_address]
            
            session.close()
            info("TCP", f"[{session._log_prefix()}] Клиент {session.client_address[0]}:{session.client_address[1]} отключен")
    
    def _handle_client(self, client_socket: socket.socket, client_address):
        """Обрабатывает подключение TCP клиента"""
        rx_buffer = bytes()
        client_socket.settimeout(0.1)
        
        try:
            while self.running:
                try:
                    while not self.mqtt_client.to_client_queue.empty():
                        response = self.mqtt_client.to_client_queue.get_nowait()
                        
                        try:
                            from_radio_data = StreamAPI.remove_framing(response)
                            if from_radio_data:
                                from_radio = mesh_pb2.FromRadio()
                                from_radio.ParseFromString(from_radio_data)
                                if from_radio.HasField('packet'):
                                    self._handle_mqtt_packet(from_radio.packet)
                        except:
                            pass
                        
                        client_socket.send(response)
                        # Логируем только важные пакеты, не каждый
                        # debug("TCP", f"Отправлен пакет клиенту ({len(response)} байт)")
                except queue.Empty:
                    pass
                except Exception as e:
                    error("TCP", f"Ошибка отправки пакета клиенту: {e}")
                
                try:
                    data = client_socket.recv(4096)
                    if not data:
                        break
                    
                    rx_buffer += data
                    
                    while len(rx_buffer) >= HEADER_LEN:
                        if rx_buffer[0] != START1 or rx_buffer[1] != START2:
                            rx_buffer = rx_buffer[1:]
                            continue
                        
                        length = struct.unpack('>H', rx_buffer[2:4])[0]
                        if length > MAX_TO_FROM_RADIO_SIZE:
                            rx_buffer = rx_buffer[1:]
                            continue
                        
                        if len(rx_buffer) < HEADER_LEN + length:
                            break
                        
                        payload = rx_buffer[HEADER_LEN:HEADER_LEN + length]
                        rx_buffer = rx_buffer[HEADER_LEN + length:]
                        
                        self._handle_to_radio(payload)
                
                except socket.timeout:
                    continue
                except Exception as e:
                    error("TCP", f"Ошибка чтения от клиента: {e}")
                    break
        
        except Exception as e:
            error("TCP", f"Ошибка обработки клиента: {e}")
        finally:
            client_socket.close()
            info("TCP", f"Клиент {client_address[0]}:{client_address[1]} отключен")
    
    def _handle_to_radio(self, payload: bytes):
        """Обрабатывает ToRadio сообщение"""
        try:
            to_radio = mesh_pb2.ToRadio()
            to_radio.ParseFromString(payload)
            
            msg_type = to_radio.WhichOneof('payload_variant')
            debug("TCP", f"Получен ToRadio: {msg_type}")
            
            if to_radio.HasField('want_config_id'):
                debug("TCP", f"Запрос конфигурации с want_config_id={to_radio.want_config_id}")
                self._send_config(to_radio.want_config_id)
            elif to_radio.HasField('packet'):
                debug("TCP", f"ToRadio содержит MeshPacket")
                self._handle_mesh_packet(to_radio.packet)
        except Exception as e:
            error("TCP", f"Ошибка обработки ToRadio: {e}")
            import traceback
            traceback.print_exc()
    
    def _handle_mesh_packet(self, packet: mesh_pb2.MeshPacket):
        """Обрабатывает MeshPacket"""
        try:
            # В firmware пакеты от клиента имеют from=0 (локальный узел)
            # Но мы не должны перезаписывать from, если он уже установлен
            # Это важно для правильной обработки ответов
            
            if packet.id == 0:
                packet.id = random.randint(1, 0xFFFFFFFF)
            
            # Устанавливаем hop_limit и hop_start для пакетов от клиента (как в firmware ReliableRouter::send и Router::send)
            # Если hop_limit не установлен или 0, и want_ack=True, устанавливаем дефолтный (как в firmware ReliableRouter::send)
            want_ack = getattr(packet, 'want_ack', False)
            hop_limit = getattr(packet, 'hop_limit', 0)
            if want_ack and hop_limit == 0:
                hop_limit = DEFAULT_HOP_LIMIT
                packet.hop_limit = hop_limit
                debug("TCP", f"Установлен дефолтный hop_limit={hop_limit} для пакета с want_ack=True")
            
            # Если hop_limit установлен, устанавливаем hop_start = hop_limit (как в firmware Router::send для isFromUs)
            if hop_limit > 0:
                hop_start = getattr(packet, 'hop_start', 0)
                if hop_start == 0:
                    packet.hop_start = hop_limit
                    debug("TCP", f"Установлен hop_start={hop_limit} для исходящего пакета")
            
            payload_type = packet.WhichOneof('payload_variant')
            packet_from = getattr(packet, 'from', 0)
            hop_limit = getattr(packet, 'hop_limit', 0)
            hop_start = getattr(packet, 'hop_start', 0)
            
            # Логируем информацию о трассировке маршрута
            hops_away = 0
            if hop_start != 0 and hop_limit <= hop_start:
                hops_away = hop_start - hop_limit
                if hops_away > 0:
                    debug("TCP", f"Трассировка маршрута: hops_away={hops_away}, hop_start={hop_start}, hop_limit={hop_limit}")
            
            packet_to = packet.to
            debug("TCP", f"Получен MeshPacket: payload_variant={payload_type}, id={packet.id}, from={packet_from:08X}, to={packet_to:08X}, channel={packet.channel}, want_ack={want_ack}, hop_limit={hop_limit}, hop_start={hop_start}, hops_away={hops_away}")
            
            if (payload_type == 'decoded' and 
                hasattr(packet.decoded, 'portnum') and
                packet.decoded.portnum == portnums_pb2.PortNum.ADMIN_APP):
                debug("TCP", f"AdminMessage обнаружен, передача в обработчик (want_response={getattr(packet.decoded, 'want_response', False)})")
                self._handle_admin_message(packet)
            
            channel_index = packet.channel if packet.channel < MAX_NUM_CHANNELS else 0
            self.mqtt_client.publish_packet(packet, channel_index)
            
            # Отправляем ACK если запрошено (как в firmware ReliableRouter::sniffReceived)
            # В firmware ACK отправляется для всех decoded пакетов с want_ack=True, 
            # кроме самих Routing пакетов с request_id (которые уже являются ACK)
            # ВАЖНО: ACK должен отправляться ПОСЛЕ обработки Admin сообщений, 
            # но ПЕРЕД публикацией в MQTT (так как Admin пакеты не публикуются)
            if want_ack and payload_type == 'decoded':
                # Проверяем, что это не ACK сам по себе (Routing сообщение с request_id)
                is_routing_ack = (hasattr(packet.decoded, 'portnum') and 
                                 packet.decoded.portnum == portnums_pb2.PortNum.ROUTING_APP and
                                 hasattr(packet.decoded, 'request_id') and 
                                 packet.decoded.request_id != 0)
                
                # В firmware ACK отправляется для всех пакетов с want_ack=True, включая Admin сообщения
                # Admin сообщения могут иметь want_response=True (ответное Admin сообщение) и want_ack=True (ACK) одновременно
                # ACK отправляется независимо от want_response
                
                # Отправляем ACK для всех decoded пакетов, кроме самих Routing ACK
                if not is_routing_ack:
                    portnum_name = packet.decoded.portnum if hasattr(packet.decoded, 'portnum') else 'N/A'
                    debug("ACK", f"Отправка ACK для пакета {packet.id} (portnum={portnum_name}, from={packet_from:08X})")
                    self._send_ack(packet, channel_index)
                else:
                    debug("ACK", f"Пропуск ACK для Routing пакета {packet.id} (это уже ACK)")
        except Exception as e:
            error("TCP", f"Ошибка обработки MeshPacket: {e}")
            import traceback
            traceback.print_exc()
    
    def _handle_mqtt_packet(self, packet: mesh_pb2.MeshPacket):
        """Обрабатывает MeshPacket полученный из MQTT"""
        try:
            # Устанавливаем rx_time при получении пакета (как в firmware Router::handleReceived)
            if hasattr(packet, 'rx_time'):
                rx_time = get_valid_time(RTCQuality.FROM_NET)
                if rx_time > 0:
                    packet.rx_time = rx_time
            
            self.node_db.update_from(packet)
            
            if packet.WhichOneof('payload_variant') == 'decoded' and hasattr(packet.decoded, 'portnum'):
                packet_from = getattr(packet, 'from', 0)
                
                if packet.decoded.portnum == portnums_pb2.PortNum.NODEINFO_APP:
                    try:
                        user = mesh_pb2.User()
                        user.ParseFromString(packet.decoded.payload)
                        self.node_db.update_user(packet_from, user, packet.channel)
                    except Exception as e:
                        error("NODE", f"Ошибка обновления информации о пользователе: {e}")
                
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
                        import traceback
                        traceback.print_exc()
                
                elif packet.decoded.portnum == portnums_pb2.PortNum.POSITION_APP:
                    try:
                        position = mesh_pb2.Position()
                        position.ParseFromString(packet.decoded.payload)
                        self.node_db.update_position(packet_from, position)
                    except Exception as e:
                        error("NODE", f"Ошибка обновления позиции: {e}")
        except Exception as e:
            error("MQTT", f"Ошибка обработки MeshPacket из MQTT: {e}")
            import traceback
            traceback.print_exc()
    
    def _send_ack(self, packet: mesh_pb2.MeshPacket, channel_index: int):
        """Отправляет ACK пакет обратно клиенту (как в firmware MeshModule::allocAckNak)"""
        try:
            packet_from = getattr(packet, 'from', 0)
            packet_to = packet.to
            packet_id = packet.id
            # Для TCP клиента from=0 означает локальный узел, поэтому ACK отправляем обратно клиенту через TCP
            # В firmware для локальных пакетов используется hop_limit=0
            hop_limit = 0  # TCP клиент - прямое соединение
            
            # Создаем Routing сообщение с ACK (как в firmware MeshModule::allocAckNak)
            routing_msg = mesh_pb2.Routing()
            routing_msg.error_reason = mesh_pb2.Routing.Error.NONE  # ACK = нет ошибки
            
            # Создаем MeshPacket с ACK (как в firmware MeshModule::allocAckNak)
            # ВАЖНО: Для TCP клиента ACK должен приходить от получателя (to исходного пакета),
            # чтобы клиент мог правильно сопоставить ACK с отправленным сообщением
            # В Android клиенте проверяется: fromId == p?.data?.to
            ack_packet = mesh_pb2.MeshPacket()
            ack_packet.id = random.randint(1, 0xFFFFFFFF)
            ack_packet.to = packet_from  # Отправитель исходного пакета (для TCP клиента = 0)
            
            # Для TCP клиента: если исходный пакет был отправлен на конкретный узел (to != 0 и != broadcast),
            # то ACK должен приходить от этого узла (от нас). Если to был broadcast, то ACK тоже от нас.
            # В любом случае, ACK приходит от нашего узла, так как мы получили пакет от клиента
            setattr(ack_packet, 'from', self.our_node_num)  # Устанавливаем from в наш node_num
            
            # НО: если исходный пакет был отправлен на broadcast (to=0xFFFFFFFF), то клиент ожидает,
            # что from в ACK будет соответствовать to исходного пакета для статуса RECEIVED.
            # Для broadcast это не критично, так как статус будет DELIVERED (не RECEIVED).
            # Но для прямых сообщений (to=конкретный узел) клиент проверит: fromId == p?.data?.to
            # Если to был наш node_num, то from должен быть наш node_num (что мы и делаем)
            # Если to был другой узел, то клиент не ожидает ACK от нас (это не должно происходить)
            
            ack_packet.channel = channel_index
            ack_packet.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
            ack_packet.decoded.request_id = packet_id  # КРИТИЧЕСКИ ВАЖНО: ID исходного пакета (как в firmware)
            ack_packet.decoded.payload = routing_msg.SerializeToString()
            ack_packet.priority = mesh_pb2.MeshPacket.Priority.ACK
            ack_packet.hop_limit = hop_limit
            ack_packet.want_ack = False  # ACK на ACK не нужен
            
            # Устанавливаем hop_start для ACK (как в firmware Router::send для isFromUs)
            ack_packet.hop_start = hop_limit
            
            # Для broadcast сообщений (to=0xFFFFFFFF) клиент все равно должен получить ACK,
            # но статус будет DELIVERED (не RECEIVED), так как fromId != "^all"
            # Это нормальное поведение для broadcast сообщений
            
            # Отправляем ACK асинхронно с задержкой, чтобы клиент успел сохранить пакет в базу данных
            # Это особенно важно для последовательных сообщений, когда клиент обрабатывает их по очереди
            # Клиент сохраняет пакет асинхронно после отправки, поэтому ACK должен прийти с задержкой
            def send_ack_delayed():
                time.sleep(0.1)  # 100ms задержка для обеспечения сохранения пакета в базу данных клиента
                try:
                    from_radio = mesh_pb2.FromRadio()
                    from_radio.packet.CopyFrom(ack_packet)
                    self._send_from_radio(from_radio)
                    is_broadcast = packet_to == 0xFFFFFFFF
                    debug("ACK", f"Отправлен ACK (async): packet_id={ack_packet.id}, request_id={packet_id}, to={packet_from:08X}, from={self.our_node_num:08X}, channel={channel_index}, packet_to={packet_to:08X}, broadcast={is_broadcast}, error_reason=NONE")
                except Exception as e:
                    error("ACK", f"Ошибка отправки ACK (async): {e}")
            
            # Запускаем отправку ACK в отдельном потоке, чтобы не блокировать обработку других пакетов
            ack_thread = threading.Thread(target=send_ack_delayed, daemon=True)
            ack_thread.start()
            
            debug("ACK", f"Запущена асинхронная отправка ACK для пакета {packet_id} (задержка 100ms)")
        except Exception as e:
            error("ACK", f"Ошибка отправки ACK: {e}")
            import traceback
            traceback.print_exc()
    
    def _load_settings(self):
        """Загружает сохраненные настройки при запуске"""
        try:
            info("PERSISTENCE", "Загрузка сохраненных настроек...")
            loaded_count = 0
            
            # Загружаем каналы
            saved_channels = self.persistence.load_channels()
            if saved_channels and len(saved_channels) == MAX_NUM_CHANNELS:
                info("PERSISTENCE", f"Загружено {len(saved_channels)} каналов из файла")
                # Проверяем корректность каналов перед заменой
                valid = True
                for i, ch in enumerate(saved_channels):
                    if ch.index != i:
                        warn("PERSISTENCE", f"Неверный индекс канала {i}: {ch.index}, пропускаем загрузку")
                        valid = False
                        break
                if valid:
                    self.channels.channels = saved_channels
                    # Пересчитываем hashes
                    self.channels.hashes = {}
                    loaded_count += 1
                    print(f"  ✓ Загружено {len(saved_channels)} каналов")
            else:
                if saved_channels:
                    warn("PERSISTENCE", f"Неверное количество каналов: {len(saved_channels)}, ожидалось {MAX_NUM_CHANNELS}")
                else:
                    debug("PERSISTENCE", "Сохраненные каналы не найдены, используются значения по умолчанию")
            
            # Загружаем Config
            saved_config = self.persistence.load_config()
            if saved_config:
                info("PERSISTENCE", "Загружен Config из файла")
                self.config_storage.config.CopyFrom(saved_config)
                loaded_count += 1
                print("  ✓ Загружен Config")
            else:
                debug("PERSISTENCE", "Сохраненный Config не найден, используются значения по умолчанию")
            
            # Загружаем ModuleConfig
            saved_module_config = self.persistence.load_module_config()
            if saved_module_config:
                info("PERSISTENCE", "Загружен ModuleConfig из файла")
                self.config_storage.module_config.CopyFrom(saved_module_config)
                loaded_count += 1
                print("  ✓ Загружен ModuleConfig")
            else:
                debug("PERSISTENCE", "Сохраненный ModuleConfig не найден, используются значения по умолчанию")
            
            # Загружаем Owner
            saved_owner = self.persistence.load_owner()
            if saved_owner:
                info("PERSISTENCE", f"Загружен Owner из файла: {saved_owner.long_name}/{saved_owner.short_name}")
                self.owner.CopyFrom(saved_owner)
                # Обновляем ID из node_id (как в firmware)
                self.owner.id = self.mqtt_client.node_id
                # Обновляем public_key если был сгенерирован
                if self.pki_public_key and len(self.pki_public_key) == 32:
                    self.owner.public_key = self.pki_public_key
                loaded_count += 1
                print(f"  ✓ Загружен Owner: {saved_owner.long_name}/{saved_owner.short_name}")
            else:
                debug("PERSISTENCE", "Сохраненный Owner не найден, используются значения по умолчанию")
            
            # Загружаем шаблонные сообщения
            saved_canned_messages = self.persistence.load_canned_messages()
            if saved_canned_messages is not None:
                self.canned_messages = saved_canned_messages
                # Если есть сообщения, включаем модуль (как в firmware)
                if saved_canned_messages:
                    self.config_storage.module_config.canned_message.enabled = True
                loaded_count += 1
                print(f"  ✓ Загружены шаблонные сообщения ({len(saved_canned_messages)} символов)")
            else:
                debug("PERSISTENCE", "Сохраненные шаблонные сообщения не найдены")
            
            # Обновляем настройки MQTT клиента из загруженной конфигурации (как в firmware MQTT::reconnect)
            # Если адрес/логин/пароль пустые - используются существующие настройки из аргументов командной строки
            if self.config_storage.module_config.mqtt.enabled:
                info("MQTT", "Применение настроек MQTT из конфигурации ноды...")
                self.mqtt_client.update_config(self.config_storage.module_config.mqtt)
            
            if loaded_count == 0:
                print("  ℹ Используются настройки по умолчанию (файл настроек не найден или пуст)")
            else:
                info("PERSISTENCE", f"Загрузка настроек завершена: загружено {loaded_count} компонентов")
        except Exception as e:
            error("PERSISTENCE", f"Ошибка загрузки настроек: {e}")
            import traceback
            traceback.print_exc()
            warn("PERSISTENCE", "Продолжаем работу с настройками по умолчанию")
            print(f"  ⚠ Ошибка загрузки настроек: {e}")
    
    def _handle_admin_message(self, packet: mesh_pb2.MeshPacket):
        """Обрабатывает AdminMessage из MeshPacket"""
        try:
            admin_msg = admin_pb2.AdminMessage()
            admin_msg.ParseFromString(packet.decoded.payload)
            
            msg_type = admin_msg.WhichOneof('payload_variant')
            want_response = getattr(packet.decoded, 'want_response', False)
            packet_id = packet.id
            packet_from = getattr(packet, 'from', 0)
            info("ADMIN", f"Получен запрос: {msg_type} (packet_id={packet_id}, from={packet_from:08X}, want_response={want_response})")
            
            if admin_msg.HasField('set_owner'):
                # Android клиент устанавливает информацию о владельце (как в firmware AdminModule::handleSetOwner)
                owner_data = admin_msg.set_owner
                info("ADMIN", f"Установка владельца: long_name='{owner_data.long_name}', short_name='{owner_data.short_name}'")
                
                # Обновляем информацию о владельце (как в firmware)
                if owner_data.long_name:
                    self.owner.long_name = owner_data.long_name
                if owner_data.short_name:
                    self.owner.short_name = owner_data.short_name
                # is_licensed - boolean поле без presence, просто присваиваем значение
                # В protobuf boolean поля всегда имеют значение (по умолчанию False)
                self.owner.is_licensed = owner_data.is_licensed
                # is_unmessagable - boolean поле без presence
                if hasattr(owner_data, 'is_unmessagable'):
                    self.owner.is_unmessagable = owner_data.is_unmessagable
                
                # ID всегда устанавливается из нашего node_num (как в firmware)
                self.owner.id = self.mqtt_client.node_id
                
                # Сохраняем изменения
                self.persistence.save_owner(self.owner)
                
                debug("ADMIN", f"Владелец обновлен: {self.owner.long_name}/{self.owner.short_name}")
            
            elif admin_msg.HasField('set_channel'):
                self.channels.set_channel(admin_msg.set_channel)
                # Сохраняем изменения каналов
                self.persistence.save_channels(self.channels.channels)
                info("ADMIN", f"Канал {admin_msg.set_channel.index} установлен")
            
            elif admin_msg.HasField('set_config'):
                # Android клиент устанавливает конфигурацию (как в firmware AdminModule::handleSetConfig)
                config_type = admin_msg.set_config.WhichOneof('payload_variant')
                info("ADMIN", f"Конфигурация установлена: {config_type}")
                
                # Сохраняем конфигурацию в хранилище
                self.config_storage.set_config(admin_msg.set_config)
                # Сохраняем изменения
                self.persistence.save_config(self.config_storage.config)
                debug("ADMIN", f"Конфигурация сохранена в ConfigStorage")
            
            elif admin_msg.HasField('set_module_config'):
                # Android клиент устанавливает конфигурацию модуля (как в firmware AdminModule::handleSetModuleConfig)
                module_type = admin_msg.set_module_config.WhichOneof('payload_variant')
                info("ADMIN", f"Конфигурация модуля установлена: {module_type}")
                
                # Сохраняем конфигурацию модуля в хранилище
                self.config_storage.set_module_config(admin_msg.set_module_config)
                # Сохраняем изменения
                self.persistence.save_module_config(self.config_storage.module_config)
                debug("ADMIN", f"Конфигурация модуля сохранена в ConfigStorage")
                
                # Если изменилась конфигурация MQTT, обновляем настройки MQTT клиента (как в firmware MQTT::reconnect)
                if module_type == 'mqtt':
                    info("MQTT", "Конфигурация MQTT изменена, обновляем настройки...")
                    mqtt_config = admin_msg.set_module_config.mqtt
                    # Обновляем настройки MQTT клиента (если адрес/логин/пароль пустые - используем существующие)
                    self.mqtt_client.update_config(mqtt_config)
            
            elif admin_msg.HasField('get_channel_request'):
                # Android клиент отправляет get_channel_request = index + 1 (1-based)
                # firmware использует 0-based индексы, поэтому вычитаем 1
                requested_index = admin_msg.get_channel_request
                ch_index = requested_index - 1
                
                debug("ADMIN", f"get_channel_request: {requested_index} (индекс канала: {ch_index})")
                
                # Проверяем, хочет ли клиент ответ (как в firmware)
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", f"get_channel_request без want_response (канал {ch_index})")
                    return
                
                if 0 <= ch_index < MAX_NUM_CHANNELS:
                    ch = self.channels.get_by_index(ch_index)
                    debug("ADMIN", f"Канал {ch_index}: role={ch.role}, name={ch.settings.name if ch.settings.name else 'N/A'}, index={ch.index}")
                    
                    # Создаем AdminMessage с get_channel_response (как в firmware)
                    # В firmware: r.get_channel_response = channels.getByIndex(channelIndex);
                    admin_response = admin_pb2.AdminMessage()
                    # Копируем канал, убеждаемся что index установлен правильно
                    admin_response.get_channel_response.CopyFrom(ch)
                    # Убеждаемся что index установлен (хотя должен быть уже установлен в get_by_index)
                    if admin_response.get_channel_response.index != ch_index:
                        admin_response.get_channel_response.index = ch_index
                        debug("ADMIN", f"Исправлен index канала: {admin_response.get_channel_response.index}")
                    
                    # Создаем MeshPacket с ответом (как в firmware setReplyTo)
                    reply_packet = mesh_pb2.MeshPacket()
                    reply_packet.id = random.randint(1, 0xFFFFFFFF)
                    
                    # setReplyTo логика (из firmware MeshModule.cpp:233-245)
                    # p->to = getFrom(&to) - получатель это отправитель запроса
                    packet_from = getattr(packet, 'from', 0)
                    # В firmware getFrom возвращает packet.from если != 0, иначе ourNodeNum
                    # Для TCP клиента from всегда 0, поэтому используем 0 (локальный узел)
                    reply_packet.to = packet_from  # 0 означает локальный узел для TCP
                    
                    # Устанавливаем from в наш node_num (как в firmware - пакеты от нашего устройства)
                    # Используем setattr потому что "from" - зарезервированное слово в Python
                    setattr(reply_packet, 'from', self.our_node_num)
                    
                    # p->channel = to.channel - тот же канал
                    reply_packet.channel = packet.channel
                    
                    # p->decoded.request_id = to.id - ID запроса
                    reply_packet.decoded.request_id = packet.id
                    
                    # p->want_ack = (to.from != 0) ? to.want_ack : false
                    # Для TCP клиента from=0, поэтому want_ack=False
                    reply_packet.want_ack = False
                    
                    # p->priority = meshtastic_MeshPacket_Priority_RELIABLE если не установлен
                    reply_packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
                    
                    # Устанавливаем decoded порт и payload
                    reply_packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
                    reply_packet.decoded.payload = admin_response.SerializeToString()
                    
                    # Отправляем через FromRadio.packet
                    from_radio = mesh_pb2.FromRadio()
                    from_radio.packet.CopyFrom(reply_packet)
                    self._send_from_radio(from_radio)
                    info("ADMIN", f"Отправлен ответ get_channel_response для канала {ch_index} (request_id={packet.id})")
                else:
                    warn("ADMIN", f"Неверный индекс канала: {ch_index} (запрошен: {requested_index}, максимум: {MAX_NUM_CHANNELS-1})")
            
            elif admin_msg.HasField('get_config_request'):
                # Android клиент запрашивает конфигурацию (как в firmware AdminModule::handleGetConfig)
                config_type = admin_msg.get_config_request
                debug("ADMIN", f"get_config_request: {config_type}")
                
                # Проверяем, хочет ли клиент ответ (как в firmware)
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", f"get_config_request без want_response (config_type={config_type})")
                    return
                
                # Получаем конфигурацию из хранилища
                config_response = self.config_storage.get_config(config_type)
                if config_response is None:
                    warn("ADMIN", f"Неизвестный тип конфигурации: {config_type}")
                    return
                
                # Создаем AdminMessage с get_config_response (как в firmware)
                admin_response = admin_pb2.AdminMessage()
                admin_response.get_config_response.CopyFrom(config_response)
                
                # Устанавливаем which_payload_variant (как в firmware)
                # В firmware это делается автоматически при копировании, но нужно убедиться
                
                # Создаем MeshPacket с ответом (как в firmware setReplyTo)
                reply_packet = mesh_pb2.MeshPacket()
                reply_packet.id = random.randint(1, 0xFFFFFFFF)
                
                packet_from = getattr(packet, 'from', 0)
                reply_packet.to = packet_from
                # Устанавливаем from в наш node_num (используем setattr потому что "from" - зарезервированное слово)
                setattr(reply_packet, 'from', self.our_node_num)
                reply_packet.channel = packet.channel
                reply_packet.decoded.request_id = packet.id
                reply_packet.want_ack = False
                reply_packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
                reply_packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
                reply_packet.decoded.payload = admin_response.SerializeToString()
                
                # Отправляем через FromRadio.packet
                from_radio = mesh_pb2.FromRadio()
                from_radio.packet.CopyFrom(reply_packet)
                self._send_from_radio(from_radio)
                
                # Получаем имя типа конфигурации для логирования
                try:
                    config_type_name = next((name for name, value in admin_pb2.AdminMessage.ConfigType.__dict__.items() 
                                           if isinstance(value, int) and value == config_type), f"TYPE_{config_type}")
                except:
                    config_type_name = str(config_type)
                info("ADMIN", f"Отправлен ответ get_config_response для {config_type_name} (request_id={packet.id})")
            
            elif admin_msg.HasField('get_module_config_request'):
                # Android клиент запрашивает конфигурацию модуля (как в firmware AdminModule::handleGetModuleConfig)
                module_config_type = admin_msg.get_module_config_request
                debug("ADMIN", f"get_module_config_request: {module_config_type}")
                
                # Проверяем, хочет ли клиент ответ (как в firmware)
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", f"get_module_config_request без want_response (module_config_type={module_config_type})")
                    return
                
                # Получаем конфигурацию модуля из хранилища
                module_config_response = self.config_storage.get_module_config(module_config_type)
                if module_config_response is None:
                    warn("ADMIN", f"Неизвестный тип конфигурации модуля: {module_config_type}")
                    return
                
                # Создаем AdminMessage с get_module_config_response (как в firmware)
                admin_response = admin_pb2.AdminMessage()
                admin_response.get_module_config_response.CopyFrom(module_config_response)
                
                # Создаем MeshPacket с ответом (как в firmware setReplyTo)
                reply_packet = mesh_pb2.MeshPacket()
                reply_packet.id = random.randint(1, 0xFFFFFFFF)
                
                packet_from = getattr(packet, 'from', 0)
                reply_packet.to = packet_from
                # Устанавливаем from в наш node_num (используем setattr потому что "from" - зарезервированное слово)
                setattr(reply_packet, 'from', self.our_node_num)
                reply_packet.channel = packet.channel
                reply_packet.decoded.request_id = packet.id
                reply_packet.want_ack = False
                reply_packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
                reply_packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
                reply_packet.decoded.payload = admin_response.SerializeToString()
                
                # Отправляем через FromRadio.packet
                from_radio = mesh_pb2.FromRadio()
                from_radio.packet.CopyFrom(reply_packet)
                self._send_from_radio(from_radio)
                
                # Получаем имя типа конфигурации модуля для логирования
                try:
                    module_config_type_name = next((name for name, value in admin_pb2.AdminMessage.ModuleConfigType.__dict__.items() 
                                                   if isinstance(value, int) and value == module_config_type), f"TYPE_{module_config_type}")
                except:
                    module_config_type_name = str(module_config_type)
                info("ADMIN", f"Отправлен ответ get_module_config_response для {module_config_type_name} (request_id={packet.id})")
            
            elif admin_msg.HasField('get_canned_message_module_messages_request'):
                # Android клиент запрашивает шаблонные сообщения (как в firmware CannedMessageModule::handleGetCannedMessageModuleMessages)
                debug("ADMIN", "get_canned_message_module_messages_request")
                
                # Проверяем, хочет ли клиент ответ (как в firmware)
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", "get_canned_message_module_messages_request без want_response")
                    return
                
                # Получаем шаблонные сообщения (хранятся отдельно, как в firmware)
                messages = self.canned_messages
                
                # Создаем AdminMessage с get_canned_message_module_messages_response (как в firmware)
                admin_response = admin_pb2.AdminMessage()
                admin_response.get_canned_message_module_messages_response = messages
                
                # Создаем MeshPacket с ответом (как в firmware setReplyTo)
                reply_packet = mesh_pb2.MeshPacket()
                reply_packet.id = random.randint(1, 0xFFFFFFFF)
                
                packet_from = getattr(packet, 'from', 0)
                reply_packet.to = packet_from
                setattr(reply_packet, 'from', self.our_node_num)  # Устанавливаем from в наш node_num
                reply_packet.channel = packet.channel
                reply_packet.decoded.request_id = packet.id
                reply_packet.want_ack = False
                reply_packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
                reply_packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
                reply_packet.decoded.payload = admin_response.SerializeToString()
                
                # Отправляем через FromRadio.packet
                from_radio = mesh_pb2.FromRadio()
                from_radio.packet.CopyFrom(reply_packet)
                self._send_from_radio(from_radio)
                info("ADMIN", f"Отправлен ответ get_canned_message_module_messages_response (request_id={packet.id})")
            
            elif admin_msg.HasField('set_canned_message_module_messages'):
                # Android клиент устанавливает шаблонные сообщения (как в firmware CannedMessageModule::handleSetCannedMessageModuleMessages)
                messages = admin_msg.set_canned_message_module_messages
                info("ADMIN", f"Установка шаблонных сообщений: '{messages[:50]}...' (длина: {len(messages)})")
                
                # Обновляем шаблонные сообщения (хранятся отдельно, как в firmware)
                self.canned_messages = messages
                # Если есть сообщения, включаем модуль (как в firmware)
                if messages:
                    self.config_storage.module_config.canned_message.enabled = True
                
                # Сохраняем изменения
                self.persistence.save_canned_messages(self.canned_messages)
                self.persistence.save_module_config(self.config_storage.module_config)
                debug("ADMIN", "Шаблонные сообщения сохранены")
            
            elif admin_msg.HasField('get_owner_request'):
                # Android клиент запрашивает информацию о владельце (как в firmware AdminModule::handleGetOwner)
                debug("ADMIN", "get_owner_request")
                
                # Проверяем, хочет ли клиент ответ (как в firmware)
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", "get_owner_request без want_response")
                    return
                
                # Используем сохраненную информацию о владельце (как в firmware)
                # В firmware используется глобальная переменная owner
                
                # Создаем AdminMessage с get_owner_response (как в firmware)
                admin_response = admin_pb2.AdminMessage()
                admin_response.get_owner_response.CopyFrom(self.owner)
                
                # Создаем MeshPacket с ответом (как в firmware setReplyTo)
                reply_packet = mesh_pb2.MeshPacket()
                reply_packet.id = random.randint(1, 0xFFFFFFFF)
                
                packet_from = getattr(packet, 'from', 0)
                reply_packet.to = packet_from
                setattr(reply_packet, 'from', self.our_node_num)  # Устанавливаем from в наш node_num
                reply_packet.channel = packet.channel
                reply_packet.decoded.request_id = packet.id
                reply_packet.want_ack = False
                reply_packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
                reply_packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
                reply_packet.decoded.payload = admin_response.SerializeToString()
                
                # Отправляем через FromRadio.packet
                from_radio = mesh_pb2.FromRadio()
                from_radio.packet.CopyFrom(reply_packet)
                self._send_from_radio(from_radio)
                info("ADMIN", f"Отправлен ответ get_owner_response (request_id={packet.id})")
            
            elif admin_msg.HasField('get_device_metadata_request'):
                # Android клиент запрашивает метаданные устройства (как в firmware AdminModule::handleGetDeviceMetadata)
                debug("ADMIN", "get_device_metadata_request")
                
                # Проверяем, хочет ли клиент ответ (как в firmware)
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", "get_device_metadata_request без want_response")
                    return
                
                # Создаем DeviceMetadata (как в firmware getDeviceMetadata)
                device_metadata = mesh_pb2.DeviceMetadata()
                device_metadata.firmware_version = NodeConfig.FIRMWARE_VERSION
                device_metadata.device_state_version = 1  # Версия состояния устройства
                device_metadata.canShutdown = True  # Симулятор может "выключиться"
                device_metadata.hasWifi = False  # Симулятор не имеет реального WiFi
                device_metadata.hasBluetooth = False  # Симулятор не имеет реального Bluetooth
                device_metadata.hasEthernet = True  # Симулятор использует Ethernet (TCP)
                device_metadata.role = self.config_storage.config.device.role
                device_metadata.position_flags = self.config_storage.config.position.position_flags
                try:
                    # Устанавливаем hw_model (как в firmware)
                    if NodeConfig.HW_MODEL == "PORTDUINO":
                        device_metadata.hw_model = mesh_pb2.HardwareModel.PORTDUINO
                    else:
                        hw_model_attr = getattr(mesh_pb2.HardwareModel, NodeConfig.HW_MODEL, None)
                        if hw_model_attr is not None:
                            device_metadata.hw_model = hw_model_attr
                except Exception as e:
                    debug("ADMIN", f"Не удалось установить hw_model в DeviceMetadata: {e}")
                device_metadata.hasRemoteHardware = self.config_storage.module_config.remote_hardware.enabled
                device_metadata.hasPKC = CRYPTOGRAPHY_AVAILABLE  # PKC (Public Key Cryptography) доступен если есть cryptography
                # excluded_modules - по умолчанию EXCLUDED_NONE (как в firmware)
                device_metadata.excluded_modules = mesh_pb2.ExcludedModules.EXCLUDED_NONE
                
                # Создаем AdminMessage с get_device_metadata_response (как в firmware)
                admin_response = admin_pb2.AdminMessage()
                admin_response.get_device_metadata_response.CopyFrom(device_metadata)
                
                # Создаем MeshPacket с ответом (как в firmware setReplyTo)
                reply_packet = mesh_pb2.MeshPacket()
                reply_packet.id = random.randint(1, 0xFFFFFFFF)
                
                packet_from = getattr(packet, 'from', 0)
                reply_packet.to = packet_from
                setattr(reply_packet, 'from', self.our_node_num)  # Устанавливаем from в наш node_num
                reply_packet.channel = packet.channel
                reply_packet.decoded.request_id = packet.id
                reply_packet.want_ack = False
                reply_packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
                reply_packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
                reply_packet.decoded.payload = admin_response.SerializeToString()
                
                # Отправляем через FromRadio.packet
                from_radio = mesh_pb2.FromRadio()
                from_radio.packet.CopyFrom(reply_packet)
                self._send_from_radio(from_radio)
                info("ADMIN", f"Отправлен ответ get_device_metadata_response (request_id={packet.id})")
            
            elif admin_msg.HasField('set_time_only'):
                # Этот метод больше не используется - обработка перенесена в TCPConnectionSession
                # Оставлено для совместимости, но не должно вызываться
                warn("ADMIN", "set_time_only вызван в старом методе TCPServer (должен обрабатываться в сессии)")
                
        except Exception as e:
            error("ADMIN", f"Ошибка обработки AdminMessage: {e}")
            import traceback
            traceback.print_exc()
    
    def _send_config(self, config_nonce: int):
        """Отправляет конфигурацию клиенту"""
        info("CONFIG", f"Отправка конфигурации (nonce: {config_nonce})")
        
        # 1. MyInfo
        my_info = mesh_pb2.MyNodeInfo()
        try:
            node_num = int(self.mqtt_client.node_id[1:], 16) if self.mqtt_client.node_id.startswith('!') else int(self.mqtt_client.node_id, 16)
        except:
            node_num = NodeConfig.FALLBACK_NODE_NUM
        my_info.my_node_num = node_num & 0x7FFFFFFF
        
        # Устанавливаем device_id (уникальный идентификатор устройства, 16 байт)
        # В firmware это уникальный ID чипа (ESP32 efuse, NRF52 DeviceID, etc.)
        # Для симулятора используем MAC адрес + padding до 16 байт
        if hasattr(self, '_device_id') and self._device_id:
            my_info.device_id = self._device_id
        else:
            # Генерируем device_id на основе node_id (MAC адрес)
            # Формат: первые 8 байт из MAC, остальные нули
            device_id_bytes = bytearray(16)
            try:
                # Парсим MAC из node_id (например, !FB570E5D -> 80:38:FB:57:0E:5D)
                mac_hex = self.mqtt_client.node_id[1:] if self.mqtt_client.node_id.startswith('!') else self.mqtt_client.node_id
                # MAC адрес в node_id: последние 8 символов = последние 4 байта MAC
                # Для полноты добавляем фиксированные первые 2 байта
                device_id_bytes[0] = 0x80  # Первый байт MAC (обычно 80 для локально администрируемых)
                device_id_bytes[1] = 0x38  # Второй байт MAC
                # Остальные байты из node_id
                for i in range(0, min(6, len(mac_hex) // 2)):
                    if i + 2 < len(mac_hex):
                        device_id_bytes[i + 2] = int(mac_hex[i*2:(i+1)*2], 16)
            except:
                # Fallback: используем node_num для генерации device_id
                device_id_bytes[0:4] = node_num.to_bytes(4, 'little')
                device_id_bytes[4:8] = (node_num >> 32).to_bytes(4, 'little') if node_num > 0xFFFFFFFF else b'\x00\x00\x00\x00'
            
            my_info.device_id = bytes(device_id_bytes)
            self._device_id = my_info.device_id  # Сохраняем для повторного использования
            debug("CONFIG", f"Device ID установлен: {my_info.device_id.hex()}")
        
        from_radio = mesh_pb2.FromRadio()
        from_radio.my_info.CopyFrom(my_info)
        self._send_from_radio(from_radio)
        
        # 2. NodeInfo
        node_info = mesh_pb2.NodeInfo()
        node_info.num = node_num & 0x7FFFFFFF
        # Используем сохраненную информацию о владельце (как в firmware NodeInfoModule::allocReply)
        node_info.user.id = self.owner.id
        node_info.user.long_name = self.owner.long_name
        node_info.user.short_name = self.owner.short_name
        # Копируем остальные поля из owner
        # is_licensed - boolean поле без presence, просто присваиваем значение
        node_info.user.is_licensed = self.owner.is_licensed
        if self.owner.public_key and len(self.owner.public_key) > 0:
            # В firmware для licensed пользователей public_key удаляется (NodeInfoModule::allocReply)
            if not self.owner.is_licensed:
                node_info.user.public_key = self.owner.public_key
        try:
            # Преобразуем строку в enum
            if NodeConfig.HW_MODEL == "PORTDUINO":
                node_info.user.hw_model = mesh_pb2.HardwareModel.PORTDUINO
            else:
                # Пытаемся найти по имени
                hw_model_attr = getattr(mesh_pb2.HardwareModel, NodeConfig.HW_MODEL, None)
                if hw_model_attr is not None:
                    node_info.user.hw_model = hw_model_attr
                else:
                    # Fallback на PORTDUINO
                    node_info.user.hw_model = mesh_pb2.HardwareModel.PORTDUINO
        except Exception as e:
            debug("CONFIG", f"Не удалось установить hw_model: {e}")
        
        if self.pki_public_key and len(self.pki_public_key) == 32:
            node_info.user.public_key = self.pki_public_key
            info("PKI", f"Публичный ключ добавлен в NodeInfo ({self.pki_public_key[:8].hex()}...)")
        else:
            warn("PKI", f"Публичный ключ не доступен (размер: {len(self.pki_public_key) if self.pki_public_key else 0})")
        
        if telemetry_pb2:
            try:
                our_node = self.node_db.get_mesh_node(node_num)
                if our_node and hasattr(our_node, 'device_metrics'):
                    node_info.device_metrics.CopyFrom(our_node.device_metrics)
                else:
                    device_metrics = telemetry_pb2.DeviceMetrics()
                    device_metrics.battery_level = NodeConfig.DEVICE_METRICS_BATTERY_LEVEL  # 101 = powered (pwd)
                    device_metrics.voltage = NodeConfig.DEVICE_METRICS_VOLTAGE
                    device_metrics.channel_utilization = NodeConfig.DEVICE_METRICS_CHANNEL_UTILIZATION
                    device_metrics.air_util_tx = NodeConfig.DEVICE_METRICS_AIR_UTIL_TX
                    # Используем RTC время для uptime, если доступно (как в firmware)
                    current_time = get_valid_time(RTCQuality.FROM_NET)
                    if current_time > 0:
                        # uptime_seconds - время с момента последнего включения (упрощенно)
                        device_metrics.uptime_seconds = current_time % (365 * 24 * 3600)
                    else:
                        # Fallback на текущее время если RTC не установлен
                        device_metrics.uptime_seconds = int(time.time()) % (365 * 24 * 3600)
                    node_info.device_metrics.CopyFrom(device_metrics)
                    
                    if our_node:
                        our_node.device_metrics.CopyFrom(device_metrics)
                    else:
                        our_node = self.node_db.get_or_create_mesh_node(node_num)
                        our_node.device_metrics.CopyFrom(device_metrics)
            except Exception as e:
                error("CONFIG", f"Ошибка добавления device_metrics: {e}")
                import traceback
                traceback.print_exc()
        
        # В firmware last_heard устанавливается при обновлении NodeInfo
        # Но для нашего собственного узла это не критично
        
        from_radio = mesh_pb2.FromRadio()
        from_radio.node_info.CopyFrom(node_info)
        self._send_from_radio(from_radio)
        
        # 3. Metadata (как в firmware getDeviceMetadata)
        metadata = mesh_pb2.DeviceMetadata()
        metadata.firmware_version = NodeConfig.FIRMWARE_VERSION
        metadata.device_state_version = 1  # Версия состояния устройства
        metadata.canShutdown = True  # Симулятор может "выключиться" (camelCase в protobuf)
        metadata.hasWifi = False  # Симулятор не имеет реального WiFi
        metadata.hasBluetooth = False  # Симулятор не имеет реального Bluetooth
        metadata.hasEthernet = True  # Симулятор использует Ethernet (TCP)
        metadata.role = self.config_storage.config.device.role
        metadata.position_flags = self.config_storage.config.position.position_flags
        try:
            # Устанавливаем hw_model (как в firmware)
            if NodeConfig.HW_MODEL == "PORTDUINO":
                metadata.hw_model = mesh_pb2.HardwareModel.PORTDUINO
            else:
                hw_model_attr = getattr(mesh_pb2.HardwareModel, NodeConfig.HW_MODEL, None)
                if hw_model_attr is not None:
                    metadata.hw_model = hw_model_attr
        except Exception as e:
            debug("CONFIG", f"Не удалось установить hw_model в DeviceMetadata: {e}")
        metadata.hasRemoteHardware = self.config_storage.module_config.remote_hardware.enabled
        metadata.hasPKC = CRYPTOGRAPHY_AVAILABLE  # PKC (Public Key Cryptography) доступен если есть cryptography
        
        from_radio = mesh_pb2.FromRadio()
        from_radio.metadata.CopyFrom(metadata)
        self._send_from_radio(from_radio)
        
        # 4. Channels
        for i in range(MAX_NUM_CHANNELS):
            channel = self.channels.get_by_index(i)
            from_radio = mesh_pb2.FromRadio()
            from_radio.channel.CopyFrom(channel)
            self._send_from_radio(from_radio)
        
        # 5. Other NodeInfos
        all_nodes = self.node_db.get_all_nodes()
        if all_nodes:
            info("CONFIG", f"Отправка информации о {len(all_nodes)} узлах")
            for node_info in all_nodes:
                from_radio = mesh_pb2.FromRadio()
                from_radio.node_info.CopyFrom(node_info)
                self._send_from_radio(from_radio)
        
        # 6. Config complete
        from_radio = mesh_pb2.FromRadio()
        from_radio.config_complete_id = config_nonce
        self._send_from_radio(from_radio)
    
    def _send_from_radio(self, from_radio: mesh_pb2.FromRadio):
        """Отправляет FromRadio сообщение в очередь"""
        framed = StreamAPI.add_framing(from_radio.SerializeToString())
        self.mqtt_client.to_client_queue.put(framed)
    
    def stop(self):
        """Останавливает TCP сервер"""
        self.running = False
        if self.server_socket:
            self.server_socket.close()

