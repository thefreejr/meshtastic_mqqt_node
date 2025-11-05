"""
TCP сессия для каждого подключенного клиента
"""

import socket
import time
import random
import threading
from datetime import datetime
from typing import Optional

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

from ..config import MAX_NUM_CHANNELS, NodeConfig, DEFAULT_HOP_LIMIT
from ..mesh.channels import Channels
from ..mesh.node_db import NodeDB
from ..mesh.config_storage import ConfigStorage
from ..mesh.persistence import Persistence
from ..mesh.rtc import RTC, RTCQuality, RTCSetResult, get_valid_time
from ..mqtt.client import MQTTClient
from ..mesh import generate_node_id
from ..protocol.stream_api import StreamAPI
from ..utils.logger import info, debug, error, warn

try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False


class TCPConnectionSession:
    """Сессия для одного TCP подключения"""
    
    def __init__(self, client_socket: socket.socket, client_address: tuple, node_id: Optional[str] = None, server=None):
        """
        Инициализирует сессию для TCP клиента
        
        Args:
            client_socket: TCP сокет клиента
            client_address: Адрес клиента (host, port)
            node_id: Node ID для этой сессии (если None - генерируется автоматически)
            server: Ссылка на TCPServer для доступа к маппингу device_id
        """
        self.client_socket = client_socket
        self.client_address = client_address
        self.created_at = time.time()  # Для uptime_seconds
        self.server = server  # Ссылка на сервер для обновления маппинга
        
        # Генерируем или используем переданный node_id
        self.node_id = node_id or generate_node_id()
        self.original_node_id = self.node_id  # Сохраняем оригинальный для возможного обновления
        
        # Определяем node_num из node_id
        try:
            self.node_num = int(self.node_id[1:], 16) if self.node_id.startswith('!') else int(self.node_id, 16)
            self.node_num = self.node_num & 0x7FFFFFFF  # Убираем знак
        except:
            self.node_num = NodeConfig.FALLBACK_NODE_NUM
        
        # Индивидуальные компоненты для этой сессии
        self.config_storage = ConfigStorage()
        self.channels = Channels()
        self.node_db = NodeDB(our_node_num=self.node_num)
        self.rtc = RTC()  # Индивидуальный RTC
        self.persistence = Persistence(node_id=self.node_id)  # Уникальный файл для каждой сессии
        self.canned_messages = ""  # Шаблонные сообщения
        
        # PKI ключи (Curve25519) - индивидуальные для каждой сессии
        self.pki_private_key = None
        self.pki_public_key = None
        self._generate_pki_keys()
        
        # Хранилище информации о владельце (owner) - должно быть создано до логирования
        self.owner = mesh_pb2.User()
        self.owner.id = self.node_id
        self.owner.long_name = NodeConfig.USER_LONG_NAME
        self.owner.short_name = NodeConfig.USER_SHORT_NAME
        self.owner.is_licensed = False
        if self.pki_public_key and len(self.pki_public_key) == 32:
            self.owner.public_key = self.pki_public_key
        
        info("SESSION", f"Создана сессия: node_id={self.node_id}, node_num={self.node_num:08X}, address={client_address[0]}:{client_address[1]}, owner={self.owner.short_name or self.owner.long_name or 'N/A'}")
        
        # MQTT клиент будет создан при необходимости (ленивая инициализация)
        self.mqtt_client: Optional[MQTTClient] = None
        
        # Флаг для отслеживания отправки config
        self.config_sent_nodes = False
    
    def _log_prefix(self) -> str:
        """Возвращает префикс для логов с node_id и именем пользователя"""
        if not hasattr(self, 'owner') or not self.owner:
            return self.node_id
        if self.owner.short_name:
            return f"{self.node_id}|{self.owner.short_name}"
        elif self.owner.long_name:
            return f"{self.node_id}|{self.owner.long_name[:10]}"
        else:
            return self.node_id
    
    def _generate_pki_keys(self):
        """Генерирует Curve25519 ключи для PKI"""
        if not CRYPTOGRAPHY_AVAILABLE:
            self.pki_private_key = bytes(32)
            self.pki_public_key = bytes(32)
            return
        
        try:
            private_key_obj = X25519PrivateKey.generate()
            self.pki_private_key = private_key_obj.private_bytes_raw()
            public_key_obj = private_key_obj.public_key()
            self.pki_public_key = public_key_obj.public_bytes_raw()
            debug("PKI", f"[{self._log_prefix()}] Ключи сгенерированы (public_key: {self.pki_public_key[:8].hex()}...)")
        except Exception as e:
            error("PKI", f"[{self._log_prefix()}] Ошибка генерации ключей: {e}")
            import traceback
            traceback.print_exc()
            self.pki_private_key = bytes(32)
            self.pki_public_key = bytes(32)
    
    def get_or_create_mqtt_client(self, default_broker: str, default_port: int, 
                                   default_username: str, default_password: str,
                                   default_root: str) -> MQTTClient:
        """
        Получает или создает MQTT клиент для этой сессии
        
        Args:
            default_broker: Дефолтный MQTT брокер
            default_port: Дефолтный порт
            default_username: Дефолтный username
            default_password: Дефолтный password
            default_root: Дефолтный root topic
            
        Returns:
            MQTTClient: MQTT клиент для этой сессии
        """
        if self.mqtt_client is None:
            # Создаем MQTT клиент с настройками из module_config или дефолтными
            # ВАЖНО: Настройки уже должны быть загружены из файла (_load_settings вызывается перед этим)
            mqtt_config = self.config_storage.module_config.mqtt
            
            # Используем настройки из module_config (которые уже загружены из файла) или дефолтные
            broker = default_broker
            port = default_port
            username = default_username
            password = default_password
            root = default_root
            
            # Если MQTT включен и есть настройки в module_config, используем их
            if mqtt_config.enabled:
                if hasattr(mqtt_config, 'address') and mqtt_config.address:
                    address = mqtt_config.address.strip()
                    if address:
                        if ':' in address:
                            parts = address.split(':')
                            broker = parts[0]
                            try:
                                port = int(parts[1])
                            except:
                                port = 8883 if mqtt_config.tls_enabled else 1883
                        else:
                            broker = address
                            port = 8883 if mqtt_config.tls_enabled else 1883
                        info("MQTT", f"[{self._log_prefix()}] Используем настройки MQTT из module_config: {broker}:{port}")
                
                if hasattr(mqtt_config, 'username') and mqtt_config.username:
                    username = mqtt_config.username.strip() or default_username
                
                if hasattr(mqtt_config, 'password') and mqtt_config.password:
                    password = mqtt_config.password.strip() or default_password
                
                if hasattr(mqtt_config, 'root') and mqtt_config.root:
                    root = mqtt_config.root.strip() or default_root
            else:
                info("MQTT", f"[{self._log_prefix()}] MQTT отключен в module_config, используем дефолтные настройки")
            
            self.mqtt_client = MQTTClient(
                broker=broker,
                port=port,
                username=username,
                password=password,
                root_topic=root,
                node_id=self.node_id,
                channels=self.channels,
                node_db=self.node_db
            )
            
            if not self.mqtt_client.start():
                error("MQTT", f"[{self._log_prefix()}] Не удалось подключиться к MQTT")
                return None
            
            info("MQTT", f"[{self._log_prefix()}] MQTT клиент создан: {broker}:{port}")
        
        return self.mqtt_client
    
    def get_uptime_seconds(self) -> int:
        """Возвращает uptime в секундах для этой сессии"""
        return int(time.time() - self.created_at)
    
    def _send_from_radio(self, from_radio):
        """Отправляет FromRadio сообщение клиенту через TCP сокет"""
        try:
            framed = StreamAPI.add_framing(from_radio.SerializeToString())
            self.client_socket.send(framed)
        except Exception as e:
            error("TCP", f"[{self._log_prefix()}] Ошибка отправки FromRadio: {e}")
    
    def _load_settings(self):
        """Загружает сохраненные настройки при запуске"""
        try:
            info("PERSISTENCE", f"[{self._log_prefix()}] Загрузка сохраненных настроек...")
            loaded_count = 0
            
            # Загружаем каналы
            saved_channels = self.persistence.load_channels()
            if saved_channels and len(saved_channels) == MAX_NUM_CHANNELS:
                info("PERSISTENCE", f"[{self._log_prefix()}] Загружено {len(saved_channels)} каналов из файла")
                # Проверяем корректность каналов перед заменой
                valid = True
                for i, ch in enumerate(saved_channels):
                    if ch.index != i:
                        warn("PERSISTENCE", f"[{self._log_prefix()}] Неверный индекс канала {i}: {ch.index}, пропускаем загрузку")
                        valid = False
                        break
                if valid:
                    self.channels.channels = saved_channels
                    # Пересчитываем hashes
                    self.channels.hashes = {}
                    loaded_count += 1
                    # Логируем статус Custom канала для диагностики
                    if len(saved_channels) > 1:
                        custom_ch = saved_channels[1]
                        info("PERSISTENCE", f"[{self._log_prefix()}] Загружено {len(saved_channels)} каналов. Custom канал (index=1): downlink_enabled={custom_ch.settings.downlink_enabled}, uplink_enabled={custom_ch.settings.uplink_enabled}")
                    else:
                        debug("PERSISTENCE", f"[{self._log_prefix()}] Загружено {len(saved_channels)} каналов")
            else:
                if saved_channels:
                    warn("PERSISTENCE", f"[{self._log_prefix()}] Неверное количество каналов: {len(saved_channels)}, ожидалось {MAX_NUM_CHANNELS}")
                else:
                    debug("PERSISTENCE", f"[{self._log_prefix()}] Сохраненные каналы не найдены, используются значения по умолчанию")
            
            # Загружаем Config
            saved_config = self.persistence.load_config()
            if saved_config:
                info("PERSISTENCE", f"[{self._log_prefix()}] Загружен Config из файла")
                self.config_storage.config.CopyFrom(saved_config)
                loaded_count += 1
            else:
                debug("PERSISTENCE", f"[{self._log_prefix()}] Сохраненный Config не найден, используются значения по умолчанию")
            
            # Загружаем ModuleConfig
            saved_module_config = self.persistence.load_module_config()
            if saved_module_config:
                info("PERSISTENCE", f"[{self._log_prefix()}] Загружен ModuleConfig из файла")
                self.config_storage.module_config.CopyFrom(saved_module_config)
                loaded_count += 1
            else:
                debug("PERSISTENCE", f"[{self._log_prefix()}] Сохраненный ModuleConfig не найден, используются значения по умолчанию")
            
            # Загружаем Owner
            saved_owner = self.persistence.load_owner()
            if saved_owner:
                info("PERSISTENCE", f"[{self._log_prefix()}] Загружен Owner из файла: {saved_owner.long_name}/{saved_owner.short_name}")
                self.owner.CopyFrom(saved_owner)
                # Обновляем ID из node_id (как в firmware)
                self.owner.id = self.node_id
                # Обновляем public_key если был сгенерирован
                if self.pki_public_key and len(self.pki_public_key) == 32:
                    self.owner.public_key = self.pki_public_key
                loaded_count += 1
            else:
                debug("PERSISTENCE", f"[{self._log_prefix()}] Сохраненный Owner не найден, используются значения по умолчанию")
            
            # Загружаем шаблонные сообщения
            saved_canned_messages = self.persistence.load_canned_messages()
            if saved_canned_messages is not None:
                self.canned_messages = saved_canned_messages
                # Если есть сообщения, включаем модуль (как в firmware)
                if saved_canned_messages:
                    self.config_storage.module_config.canned_message.enabled = True
                loaded_count += 1
            else:
                debug("PERSISTENCE", f"[{self._log_prefix()}] Сохраненные шаблонные сообщения не найдены")
            
            # ВАЖНО: MQTT клиент будет создан ПОСЛЕ загрузки настроек (в server.py),
            # поэтому здесь мы просто загружаем настройки, а клиент будет создан с правильными настройками
            # Если клиент уже создан (например, при повторной загрузке после обновления node_id),
            # применяем загруженные настройки
            if self.mqtt_client and self.config_storage.module_config.mqtt.enabled:
                # Применяем настройки MQTT из загруженного module_config
                info("PERSISTENCE", f"[{self._log_prefix()}] Применение загруженных настроек MQTT к существующему клиенту...")
                mqtt_config = self.config_storage.module_config.mqtt
                self.mqtt_client.update_config(mqtt_config)
                
                # После обновления настроек MQTT нужно переподписаться на топики
                if self.mqtt_client.connected:
                    info("PERSISTENCE", f"[{self._log_prefix()}] Переподписка на MQTT топики после обновления настроек...")
                    self.mqtt_client._send_subscriptions(self.mqtt_client.client)
            
            # ВАЖНО: После загрузки каналов нужно переподписаться на MQTT топики,
            # если клиент уже создан (например, при повторной загрузке)
            if self.mqtt_client and self.mqtt_client.connected:
                info("PERSISTENCE", f"[{self._log_prefix()}] Переподписка на MQTT топики после загрузки каналов...")
                # Проверяем Custom канал перед переподпиской
                custom_ch = self.channels.get_by_index(1)
                info("PERSISTENCE", f"[{self._log_prefix()}] Custom канал перед переподпиской: downlink_enabled={custom_ch.settings.downlink_enabled}")
                self.mqtt_client._send_subscriptions(self.mqtt_client.client)
            
            if loaded_count == 0:
                debug("PERSISTENCE", f"[{self._log_prefix()}] Используются настройки по умолчанию (файл настроек не найден или пуст)")
            else:
                info("PERSISTENCE", f"[{self._log_prefix()}] Загрузка настроек завершена: загружено {loaded_count} компонентов")
        except Exception as e:
            error("PERSISTENCE", f"[{self._log_prefix()}] Ошибка загрузки настроек: {e}")
            import traceback
            traceback.print_exc()
            warn("PERSISTENCE", f"[{self._log_prefix()}] Продолжаем работу с настройками по умолчанию")
    
    def _handle_to_radio(self, payload: bytes):
        """Обрабатывает ToRadio сообщение"""
        try:
            to_radio = mesh_pb2.ToRadio()
            to_radio.ParseFromString(payload)
            
            msg_type = to_radio.WhichOneof('payload_variant')
            debug("TCP", f"[{self._log_prefix()}] Получен ToRadio: {msg_type}")
            
            if to_radio.HasField('want_config_id'):
                debug("TCP", f"[{self._log_prefix()}] Запрос конфигурации с want_config_id={to_radio.want_config_id}")
                self._send_config(to_radio.want_config_id)
            elif to_radio.HasField('packet'):
                debug("TCP", f"[{self._log_prefix()}] ToRadio содержит MeshPacket")
                self._handle_mesh_packet(to_radio.packet)
        except Exception as e:
            error("TCP", f"[{self._log_prefix()}] Ошибка обработки ToRadio: {e}")
            import traceback
            traceback.print_exc()
    
    def _handle_mesh_packet(self, packet: mesh_pb2.MeshPacket):
        """Обрабатывает MeshPacket"""
        try:
            if packet.id == 0:
                packet.id = random.randint(1, 0xFFFFFFFF)
            
            # Устанавливаем hop_limit и hop_start для пакетов от клиента
            want_ack = getattr(packet, 'want_ack', False)
            hop_limit = getattr(packet, 'hop_limit', 0)
            if want_ack and hop_limit == 0:
                hop_limit = DEFAULT_HOP_LIMIT
                packet.hop_limit = hop_limit
                debug("TCP", f"[{self._log_prefix()}] Установлен дефолтный hop_limit={hop_limit} для пакета с want_ack=True")
            
            if hop_limit > 0:
                hop_start = getattr(packet, 'hop_start', 0)
                if hop_start == 0:
                    packet.hop_start = hop_limit
                    debug("TCP", f"[{self._log_prefix()}] Установлен hop_start={hop_limit} для исходящего пакета")
            
            payload_type = packet.WhichOneof('payload_variant')
            packet_from = getattr(packet, 'from', 0)
            hop_limit = getattr(packet, 'hop_limit', 0)
            hop_start = getattr(packet, 'hop_start', 0)
            
            # Логируем информацию о трассировке маршрута
            hops_away = 0
            if hop_start != 0 and hop_limit <= hop_start:
                hops_away = hop_start - hop_limit
                if hops_away > 0:
                    debug("TCP", f"[{self._log_prefix()}] Трассировка маршрута: hops_away={hops_away}, hop_start={hop_start}, hop_limit={hop_limit}")
            
            packet_to = packet.to
            debug("TCP", f"[{self._log_prefix()}] Получен MeshPacket: payload_variant={payload_type}, id={packet.id}, from={packet_from:08X}, to={packet_to:08X}, channel={packet.channel}, want_ack={want_ack}, hop_limit={hop_limit}, hop_start={hop_start}, hops_away={hops_away}")
            
            if (payload_type == 'decoded' and 
                hasattr(packet.decoded, 'portnum') and
                packet.decoded.portnum == portnums_pb2.PortNum.ADMIN_APP):
                debug("TCP", f"[{self._log_prefix()}] AdminMessage обнаружен, передача в обработчик (want_response={getattr(packet.decoded, 'want_response', False)})")
                self._handle_admin_message(packet)
            
            channel_index = packet.channel if packet.channel < MAX_NUM_CHANNELS else 0
            if self.mqtt_client:
                self.mqtt_client.publish_packet(packet, channel_index)
            
            # Отправляем ACK если запрошено
            if want_ack and payload_type == 'decoded':
                is_routing_ack = (hasattr(packet.decoded, 'portnum') and 
                                 packet.decoded.portnum == portnums_pb2.PortNum.ROUTING_APP and
                                 hasattr(packet.decoded, 'request_id') and 
                                 packet.decoded.request_id != 0)
                
                if not is_routing_ack:
                    portnum_name = packet.decoded.portnum if hasattr(packet.decoded, 'portnum') else 'N/A'
                    debug("ACK", f"[{self._log_prefix()}] Отправка ACK для пакета {packet.id} (portnum={portnum_name}, from={packet_from:08X})")
                    self._send_ack(packet, channel_index)
                else:
                    debug("ACK", f"[{self._log_prefix()}] Пропуск ACK для Routing пакета {packet.id} (это уже ACK)")
        except Exception as e:
            error("TCP", f"[{self._log_prefix()}] Ошибка обработки MeshPacket: {e}")
            import traceback
            traceback.print_exc()
    
    def _handle_mqtt_packet(self, packet: mesh_pb2.MeshPacket):
        """Обрабатывает MeshPacket полученный из MQTT"""
        try:
            # Устанавливаем rx_time при получении пакета (используем RTC этой сессии)
            if hasattr(packet, 'rx_time'):
                rx_time = self.rtc.get_valid_time(RTCQuality.FROM_NET)
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
                        error("NODE", f"[{self._log_prefix()}] Ошибка обновления информации о пользователе: {e}")
                
                elif packet.decoded.portnum == portnums_pb2.PortNum.TELEMETRY_APP:
                    try:
                        if telemetry_pb2:
                            telemetry = telemetry_pb2.Telemetry()
                            telemetry.ParseFromString(packet.decoded.payload)
                            variant = telemetry.WhichOneof('variant')
                            if variant == 'device_metrics':
                                self.node_db.update_telemetry(packet_from, telemetry.device_metrics)
                    except Exception as e:
                        error("NODE", f"[{self._log_prefix()}] Ошибка обновления telemetry: {e}")
                        import traceback
                        traceback.print_exc()
                
                elif packet.decoded.portnum == portnums_pb2.PortNum.POSITION_APP:
                    try:
                        position = mesh_pb2.Position()
                        position.ParseFromString(packet.decoded.payload)
                        self.node_db.update_position(packet_from, position)
                    except Exception as e:
                        error("NODE", f"[{self._log_prefix()}] Ошибка обновления позиции: {e}")
        except Exception as e:
            error("MQTT", f"[{self._log_prefix()}] Ошибка обработки MeshPacket из MQTT: {e}")
            import traceback
            traceback.print_exc()
    
    def _send_ack(self, packet: mesh_pb2.MeshPacket, channel_index: int):
        """Отправляет ACK пакет обратно клиенту"""
        try:
            packet_from = getattr(packet, 'from', 0)
            packet_to = packet.to
            packet_id = packet.id
            hop_limit = 0  # TCP клиент - прямое соединение
            
            routing_msg = mesh_pb2.Routing()
            routing_msg.error_reason = mesh_pb2.Routing.Error.NONE
            
            ack_packet = mesh_pb2.MeshPacket()
            ack_packet.id = random.randint(1, 0xFFFFFFFF)
            ack_packet.to = packet_from
            setattr(ack_packet, 'from', self.node_num)
            ack_packet.channel = channel_index
            ack_packet.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
            ack_packet.decoded.request_id = packet_id
            ack_packet.decoded.payload = routing_msg.SerializeToString()
            ack_packet.priority = mesh_pb2.MeshPacket.Priority.ACK
            ack_packet.hop_limit = hop_limit
            ack_packet.want_ack = False
            ack_packet.hop_start = hop_limit
            
            def send_ack_delayed():
                time.sleep(0.1)  # 100ms задержка
                try:
                    from_radio = mesh_pb2.FromRadio()
                    from_radio.packet.CopyFrom(ack_packet)
                    self._send_from_radio(from_radio)
                    is_broadcast = packet_to == 0xFFFFFFFF
                    debug("ACK", f"[{self._log_prefix()}] Отправлен ACK (async): packet_id={ack_packet.id}, request_id={packet_id}, to={packet_from:08X}, from={self.node_num:08X}, channel={channel_index}, packet_to={packet_to:08X}, broadcast={is_broadcast}, error_reason=NONE")
                except Exception as e:
                    error("ACK", f"[{self._log_prefix()}] Ошибка отправки ACK (async): {e}")
            
            import threading
            ack_thread = threading.Thread(target=send_ack_delayed, daemon=True)
            ack_thread.start()
            
            debug("ACK", f"[{self._log_prefix()}] Запущена асинхронная отправка ACK для пакета {packet_id} (задержка 100ms)")
        except Exception as e:
            error("ACK", f"[{self._log_prefix()}] Ошибка отправки ACK: {e}")
            import traceback
            traceback.print_exc()
    
    def _handle_admin_message(self, packet: mesh_pb2.MeshPacket):
        """Обрабатывает AdminMessage из MeshPacket"""
        try:
            admin_msg = admin_pb2.AdminMessage()
            admin_msg.ParseFromString(packet.decoded.payload)
            
            msg_type = admin_msg.WhichOneof('payload_variant')
            want_response = getattr(packet.decoded, 'want_response', False)
            packet_id = packet.id
            packet_from = getattr(packet, 'from', 0)
            info("ADMIN", f"[{self._log_prefix()}] Получен запрос: {msg_type} (packet_id={packet_id}, from={packet_from:08X}, want_response={want_response})")
            
            if admin_msg.HasField('set_owner'):
                owner_data = admin_msg.set_owner
                info("ADMIN", f"[{self._log_prefix()}] Установка владельца: long_name='{owner_data.long_name}', short_name='{owner_data.short_name}'")
                
                if owner_data.long_name:
                    self.owner.long_name = owner_data.long_name
                if owner_data.short_name:
                    self.owner.short_name = owner_data.short_name
                self.owner.is_licensed = owner_data.is_licensed
                if hasattr(owner_data, 'is_unmessagable'):
                    self.owner.is_unmessagable = owner_data.is_unmessagable
                
                self.owner.id = self.node_id
                self.persistence.save_owner(self.owner)
                debug("ADMIN", f"[{self._log_prefix()}] Владелец обновлен: {self.owner.long_name}/{self.owner.short_name}")
            
            elif admin_msg.HasField('set_channel'):
                self.channels.set_channel(admin_msg.set_channel)
                self.persistence.save_channels(self.channels.channels)
                info("ADMIN", f"[{self._log_prefix()}] Канал {admin_msg.set_channel.index} установлен")
            
            elif admin_msg.HasField('set_config'):
                config_type = admin_msg.set_config.WhichOneof('payload_variant')
                info("ADMIN", f"[{self._log_prefix()}] Конфигурация установлена: {config_type}")
                self.config_storage.set_config(admin_msg.set_config)
                self.persistence.save_config(self.config_storage.config)
                debug("ADMIN", f"[{self._log_prefix()}] Конфигурация сохранена в ConfigStorage")
            
            elif admin_msg.HasField('set_module_config'):
                module_type = admin_msg.set_module_config.WhichOneof('payload_variant')
                info("ADMIN", f"[{self._log_prefix()}] Конфигурация модуля установлена: {module_type}")
                self.config_storage.set_module_config(admin_msg.set_module_config)
                self.persistence.save_module_config(self.config_storage.module_config)
                debug("ADMIN", f"[{self._log_prefix()}] Конфигурация модуля сохранена в ConfigStorage")
                
                if module_type == 'mqtt' and self.mqtt_client:
                    info("MQTT", f"[{self._log_prefix()}] Конфигурация MQTT изменена, обновляем настройки...")
                    mqtt_config = admin_msg.set_module_config.mqtt
                    self.mqtt_client.update_config(mqtt_config)
            
            elif admin_msg.HasField('get_channel_request'):
                requested_index = admin_msg.get_channel_request
                ch_index = requested_index - 1
                
                debug("ADMIN", f"[{self._log_prefix()}] get_channel_request: {requested_index} (индекс канала: {ch_index})")
                
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", f"[{self._log_prefix()}] get_channel_request без want_response (канал {ch_index})")
                    return
                
                if 0 <= ch_index < MAX_NUM_CHANNELS:
                    ch = self.channels.get_by_index(ch_index)
                    debug("ADMIN", f"[{self._log_prefix()}] Канал {ch_index}: role={ch.role}, name={ch.settings.name if ch.settings.name else 'N/A'}, index={ch.index}")
                    
                    admin_response = admin_pb2.AdminMessage()
                    admin_response.get_channel_response.CopyFrom(ch)
                    if admin_response.get_channel_response.index != ch_index:
                        admin_response.get_channel_response.index = ch_index
                        debug("ADMIN", f"[{self._log_prefix()}] Исправлен index канала: {admin_response.get_channel_response.index}")
                    
                    reply_packet = mesh_pb2.MeshPacket()
                    reply_packet.id = random.randint(1, 0xFFFFFFFF)
                    
                    packet_from = getattr(packet, 'from', 0)
                    reply_packet.to = packet_from
                    setattr(reply_packet, 'from', self.node_num)
                    reply_packet.channel = packet.channel
                    reply_packet.decoded.request_id = packet.id
                    reply_packet.want_ack = False
                    reply_packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
                    reply_packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
                    reply_packet.decoded.payload = admin_response.SerializeToString()
                    
                    from_radio = mesh_pb2.FromRadio()
                    from_radio.packet.CopyFrom(reply_packet)
                    self._send_from_radio(from_radio)
                    info("ADMIN", f"[{self._log_prefix()}] Отправлен ответ get_channel_response для канала {ch_index} (request_id={packet.id})")
                else:
                    warn("ADMIN", f"[{self._log_prefix()}] Неверный индекс канала: {ch_index} (запрошен: {requested_index}, максимум: {MAX_NUM_CHANNELS-1})")
            
            elif admin_msg.HasField('get_config_request'):
                config_type = admin_msg.get_config_request
                debug("ADMIN", f"[{self._log_prefix()}] get_config_request: {config_type}")
                
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", f"[{self._log_prefix()}] get_config_request без want_response (config_type={config_type})")
                    return
                
                config_response = self.config_storage.get_config(config_type)
                if config_response is None:
                    warn("ADMIN", f"[{self._log_prefix()}] Неизвестный тип конфигурации: {config_type}")
                    return
                
                admin_response = admin_pb2.AdminMessage()
                admin_response.get_config_response.CopyFrom(config_response)
                
                reply_packet = mesh_pb2.MeshPacket()
                reply_packet.id = random.randint(1, 0xFFFFFFFF)
                
                packet_from = getattr(packet, 'from', 0)
                reply_packet.to = packet_from
                setattr(reply_packet, 'from', self.node_num)
                reply_packet.channel = packet.channel
                reply_packet.decoded.request_id = packet.id
                reply_packet.want_ack = False
                reply_packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
                reply_packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
                reply_packet.decoded.payload = admin_response.SerializeToString()
                
                from_radio = mesh_pb2.FromRadio()
                from_radio.packet.CopyFrom(reply_packet)
                self._send_from_radio(from_radio)
                
                try:
                    config_type_name = next((name for name, value in admin_pb2.AdminMessage.ConfigType.__dict__.items() 
                                           if isinstance(value, int) and value == config_type), f"TYPE_{config_type}")
                except:
                    config_type_name = str(config_type)
                info("ADMIN", f"[{self._log_prefix()}] Отправлен ответ get_config_response для {config_type_name} (request_id={packet.id})")
            
            elif admin_msg.HasField('get_module_config_request'):
                module_config_type = admin_msg.get_module_config_request
                debug("ADMIN", f"[{self._log_prefix()}] get_module_config_request: {module_config_type}")
                
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", f"[{self._log_prefix()}] get_module_config_request без want_response (module_config_type={module_config_type})")
                    return
                
                module_config_response = self.config_storage.get_module_config(module_config_type)
                if module_config_response is None:
                    warn("ADMIN", f"[{self._log_prefix()}] Неизвестный тип конфигурации модуля: {module_config_type}")
                    return
                
                admin_response = admin_pb2.AdminMessage()
                admin_response.get_module_config_response.CopyFrom(module_config_response)
                
                reply_packet = mesh_pb2.MeshPacket()
                reply_packet.id = random.randint(1, 0xFFFFFFFF)
                
                packet_from = getattr(packet, 'from', 0)
                reply_packet.to = packet_from
                setattr(reply_packet, 'from', self.node_num)
                reply_packet.channel = packet.channel
                reply_packet.decoded.request_id = packet.id
                reply_packet.want_ack = False
                reply_packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
                reply_packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
                reply_packet.decoded.payload = admin_response.SerializeToString()
                
                from_radio = mesh_pb2.FromRadio()
                from_radio.packet.CopyFrom(reply_packet)
                self._send_from_radio(from_radio)
                
                try:
                    module_config_type_name = next((name for name, value in admin_pb2.AdminMessage.ModuleConfigType.__dict__.items() 
                                                   if isinstance(value, int) and value == module_config_type), f"TYPE_{module_config_type}")
                except:
                    module_config_type_name = str(module_config_type)
                info("ADMIN", f"[{self._log_prefix()}] Отправлен ответ get_module_config_response для {module_config_type_name} (request_id={packet.id})")
            
            elif admin_msg.HasField('get_canned_message_module_messages_request'):
                debug("ADMIN", f"[{self._log_prefix()}] get_canned_message_module_messages_request")
                
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", f"[{self._log_prefix()}] get_canned_message_module_messages_request без want_response")
                    return
                
                messages = self.canned_messages
                
                admin_response = admin_pb2.AdminMessage()
                admin_response.get_canned_message_module_messages_response = messages
                
                reply_packet = mesh_pb2.MeshPacket()
                reply_packet.id = random.randint(1, 0xFFFFFFFF)
                
                packet_from = getattr(packet, 'from', 0)
                reply_packet.to = packet_from
                setattr(reply_packet, 'from', self.node_num)
                reply_packet.channel = packet.channel
                reply_packet.decoded.request_id = packet.id
                reply_packet.want_ack = False
                reply_packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
                reply_packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
                reply_packet.decoded.payload = admin_response.SerializeToString()
                
                from_radio = mesh_pb2.FromRadio()
                from_radio.packet.CopyFrom(reply_packet)
                self._send_from_radio(from_radio)
                info("ADMIN", f"[{self._log_prefix()}] Отправлен ответ get_canned_message_module_messages_response (request_id={packet.id})")
            
            elif admin_msg.HasField('set_canned_message_module_messages'):
                messages = admin_msg.set_canned_message_module_messages
                info("ADMIN", f"[{self._log_prefix()}] Установка шаблонных сообщений: '{messages[:50]}...' (длина: {len(messages)})")
                
                self.canned_messages = messages
                if messages:
                    self.config_storage.module_config.canned_message.enabled = True
                
                self.persistence.save_canned_messages(self.canned_messages)
                self.persistence.save_module_config(self.config_storage.module_config)
                debug("ADMIN", f"[{self._log_prefix()}] Шаблонные сообщения сохранены")
            
            elif admin_msg.HasField('get_owner_request'):
                debug("ADMIN", f"[{self._log_prefix()}] get_owner_request")
                
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", f"[{self._log_prefix()}] get_owner_request без want_response")
                    return
                
                admin_response = admin_pb2.AdminMessage()
                admin_response.get_owner_response.CopyFrom(self.owner)
                
                reply_packet = mesh_pb2.MeshPacket()
                reply_packet.id = random.randint(1, 0xFFFFFFFF)
                
                packet_from = getattr(packet, 'from', 0)
                reply_packet.to = packet_from
                setattr(reply_packet, 'from', self.node_num)
                reply_packet.channel = packet.channel
                reply_packet.decoded.request_id = packet.id
                reply_packet.want_ack = False
                reply_packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
                reply_packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
                reply_packet.decoded.payload = admin_response.SerializeToString()
                
                from_radio = mesh_pb2.FromRadio()
                from_radio.packet.CopyFrom(reply_packet)
                self._send_from_radio(from_radio)
                info("ADMIN", f"[{self._log_prefix()}] Отправлен ответ get_owner_response (request_id={packet.id})")
            
            elif admin_msg.HasField('get_device_metadata_request'):
                debug("ADMIN", f"[{self._log_prefix()}] get_device_metadata_request")
                
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", f"[{self._log_prefix()}] get_device_metadata_request без want_response")
                    return
                
                device_metadata = mesh_pb2.DeviceMetadata()
                device_metadata.firmware_version = NodeConfig.FIRMWARE_VERSION
                device_metadata.device_state_version = 1
                device_metadata.canShutdown = True
                device_metadata.hasWifi = False
                device_metadata.hasBluetooth = False
                device_metadata.hasEthernet = True
                device_metadata.role = self.config_storage.config.device.role
                device_metadata.position_flags = self.config_storage.config.position.position_flags
                try:
                    if NodeConfig.HW_MODEL == "PORTDUINO":
                        device_metadata.hw_model = mesh_pb2.HardwareModel.PORTDUINO
                    else:
                        hw_model_attr = getattr(mesh_pb2.HardwareModel, NodeConfig.HW_MODEL, None)
                        if hw_model_attr is not None:
                            device_metadata.hw_model = hw_model_attr
                except Exception as e:
                    debug("ADMIN", f"[{self._log_prefix()}] Не удалось установить hw_model в DeviceMetadata: {e}")
                device_metadata.hasRemoteHardware = self.config_storage.module_config.remote_hardware.enabled
                device_metadata.hasPKC = CRYPTOGRAPHY_AVAILABLE
                device_metadata.excluded_modules = mesh_pb2.ExcludedModules.EXCLUDED_NONE
                
                admin_response = admin_pb2.AdminMessage()
                admin_response.get_device_metadata_response.CopyFrom(device_metadata)
                
                reply_packet = mesh_pb2.MeshPacket()
                reply_packet.id = random.randint(1, 0xFFFFFFFF)
                
                packet_from = getattr(packet, 'from', 0)
                reply_packet.to = packet_from
                setattr(reply_packet, 'from', self.node_num)
                reply_packet.channel = packet.channel
                reply_packet.decoded.request_id = packet.id
                reply_packet.want_ack = False
                reply_packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
                reply_packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
                reply_packet.decoded.payload = admin_response.SerializeToString()
                
                from_radio = mesh_pb2.FromRadio()
                from_radio.packet.CopyFrom(reply_packet)
                self._send_from_radio(from_radio)
                info("ADMIN", f"[{self._log_prefix()}] Отправлен ответ get_device_metadata_response (request_id={packet.id})")
            
            elif admin_msg.HasField('set_time_only'):
                timestamp = admin_msg.set_time_only
                result = self.rtc.perhaps_set_rtc(RTCQuality.NTP, timestamp, force_update=False)
                dt = datetime.fromtimestamp(timestamp)
                if result == RTCSetResult.SUCCESS:
                    info("ADMIN", f"[{self._log_prefix()}] Синхронизация времени: {dt.strftime('%Y-%m-%d %H:%M:%S')} (timestamp: {timestamp})")
                elif result == RTCSetResult.INVALID_TIME:
                    warn("ADMIN", f"[{self._log_prefix()}] Невалидное время: {dt.strftime('%Y-%m-%d %H:%M:%S')} (timestamp: {timestamp})")
                else:
                    debug("ADMIN", f"[{self._log_prefix()}] Время не установлено (качество недостаточно): {result.name}")
                
        except Exception as e:
            error("ADMIN", f"[{self._log_prefix()}] Ошибка обработки AdminMessage: {e}")
            import traceback
            traceback.print_exc()
    
    def _send_config(self, config_nonce: int):
        """Отправляет конфигурацию клиенту"""
        info("CONFIG", f"[{self._log_prefix()}] Отправка конфигурации (nonce: {config_nonce})")
        
        # 1. MyInfo
        my_info = mesh_pb2.MyNodeInfo()
        my_info.my_node_num = self.node_num & 0x7FFFFFFF
        
        # Устанавливаем device_id
        if not hasattr(self, '_device_id') or not self._device_id:
            device_id_bytes = bytearray(16)
            try:
                mac_hex = self.node_id[1:] if self.node_id.startswith('!') else self.node_id
                device_id_bytes[0] = 0x80
                device_id_bytes[1] = 0x38
                for i in range(0, min(6, len(mac_hex) // 2)):
                    if i + 2 < len(mac_hex):
                        device_id_bytes[i + 2] = int(mac_hex[i*2:(i+1)*2], 16)
            except:
                device_id_bytes[0:4] = self.node_num.to_bytes(4, 'little')
                device_id_bytes[4:8] = (self.node_num >> 32).to_bytes(4, 'little') if self.node_num > 0xFFFFFFFF else b'\x00\x00\x00\x00'
            
            self._device_id = bytes(device_id_bytes)
            debug("CONFIG", f"[{self._log_prefix()}] Device ID установлен: {self._device_id.hex()}")
            
            # Обновляем маппинг device_id -> node_id для сохранения настроек между переподключениями
            if self.server:
                device_id_hex = self._device_id.hex()
                with self.server.device_id_lock:
                    # Если device_id уже есть в маппинге, используем сохраненный node_id
                    if device_id_hex in self.server.device_id_to_node_id:
                        saved_node_id = self.server.device_id_to_node_id[device_id_hex]
                        if saved_node_id != self.node_id:
                            info("CONFIG", f"[{self._log_prefix()}] Обновление node_id: {self.node_id} -> {saved_node_id} (найден сохраненный маппинг для device_id)")
                            self.node_id = saved_node_id
                            # Обновляем node_num
                            try:
                                self.node_num = int(self.node_id[1:], 16) if self.node_id.startswith('!') else int(self.node_id, 16)
                                self.node_num = self.node_num & 0x7FFFFFFF
                            except:
                                pass
                            # Обновляем Persistence для использования правильного файла
                            self.persistence = Persistence(node_id=self.node_id)
                            # Перезагружаем настройки с правильным node_id
                            self._load_settings()
                    else:
                        # Сохраняем новый маппинг device_id -> node_id
                        self.server.device_id_to_node_id[device_id_hex] = self.node_id
                        self.server._save_device_id_mapping()
                        info("CONFIG", f"[{self._log_prefix()}] Сохранен маппинг device_id -> node_id: {device_id_hex[:16]}... -> {self.node_id}")
        
        my_info.device_id = self._device_id
        
        from_radio = mesh_pb2.FromRadio()
        from_radio.my_info.CopyFrom(my_info)
        self._send_from_radio(from_radio)
        
        # 2. NodeInfo
        node_info = mesh_pb2.NodeInfo()
        node_info.num = self.node_num & 0x7FFFFFFF
        node_info.user.id = self.owner.id
        node_info.user.long_name = self.owner.long_name
        node_info.user.short_name = self.owner.short_name
        node_info.user.is_licensed = self.owner.is_licensed
        if self.owner.public_key and len(self.owner.public_key) > 0:
            if not self.owner.is_licensed:
                node_info.user.public_key = self.owner.public_key
        try:
            if NodeConfig.HW_MODEL == "PORTDUINO":
                node_info.user.hw_model = mesh_pb2.HardwareModel.PORTDUINO
            else:
                hw_model_attr = getattr(mesh_pb2.HardwareModel, NodeConfig.HW_MODEL, None)
                if hw_model_attr is not None:
                    node_info.user.hw_model = hw_model_attr
                else:
                    node_info.user.hw_model = mesh_pb2.HardwareModel.PORTDUINO
        except Exception as e:
            debug("CONFIG", f"[{self._log_prefix()}] Не удалось установить hw_model: {e}")
        
        if self.pki_public_key and len(self.pki_public_key) == 32:
            node_info.user.public_key = self.pki_public_key
            info("PKI", f"[{self._log_prefix()}] Публичный ключ добавлен в NodeInfo ({self.pki_public_key[:8].hex()}...)")
        
        if telemetry_pb2:
            try:
                our_node = self.node_db.get_mesh_node(self.node_num)
                if our_node and hasattr(our_node, 'device_metrics'):
                    node_info.device_metrics.CopyFrom(our_node.device_metrics)
                else:
                    device_metrics = telemetry_pb2.DeviceMetrics()
                    device_metrics.battery_level = NodeConfig.DEVICE_METRICS_BATTERY_LEVEL
                    device_metrics.voltage = NodeConfig.DEVICE_METRICS_VOLTAGE
                    device_metrics.channel_utilization = NodeConfig.DEVICE_METRICS_CHANNEL_UTILIZATION
                    device_metrics.air_util_tx = NodeConfig.DEVICE_METRICS_AIR_UTIL_TX
                    current_time = self.rtc.get_time()
                    if current_time > 0:
                        device_metrics.uptime_seconds = current_time % (365 * 24 * 3600)
                    else:
                        device_metrics.uptime_seconds = self.get_uptime_seconds()
                    node_info.device_metrics.CopyFrom(device_metrics)
                    
                    if our_node:
                        our_node.device_metrics.CopyFrom(device_metrics)
                    else:
                        our_node = self.node_db.get_or_create_mesh_node(self.node_num)
                        our_node.device_metrics.CopyFrom(device_metrics)
            except Exception as e:
                error("CONFIG", f"[{self._log_prefix()}] Ошибка добавления device_metrics: {e}")
                import traceback
                traceback.print_exc()
        
        from_radio = mesh_pb2.FromRadio()
        from_radio.node_info.CopyFrom(node_info)
        self._send_from_radio(from_radio)
        
        # 3. Metadata
        metadata = mesh_pb2.DeviceMetadata()
        metadata.firmware_version = NodeConfig.FIRMWARE_VERSION
        metadata.device_state_version = 1
        metadata.canShutdown = True
        metadata.hasWifi = False
        metadata.hasBluetooth = False
        metadata.hasEthernet = True
        metadata.role = self.config_storage.config.device.role
        metadata.position_flags = self.config_storage.config.position.position_flags
        try:
            if NodeConfig.HW_MODEL == "PORTDUINO":
                metadata.hw_model = mesh_pb2.HardwareModel.PORTDUINO
            else:
                hw_model_attr = getattr(mesh_pb2.HardwareModel, NodeConfig.HW_MODEL, None)
                if hw_model_attr is not None:
                    metadata.hw_model = hw_model_attr
        except Exception as e:
            debug("CONFIG", f"[{self._log_prefix()}] Не удалось установить hw_model в DeviceMetadata: {e}")
        metadata.hasRemoteHardware = self.config_storage.module_config.remote_hardware.enabled
        metadata.hasPKC = CRYPTOGRAPHY_AVAILABLE
        
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
            info("CONFIG", f"[{self._log_prefix()}] Отправка информации о {len(all_nodes)} узлах")
            for node_info in all_nodes:
                from_radio = mesh_pb2.FromRadio()
                from_radio.node_info.CopyFrom(node_info)
                self._send_from_radio(from_radio)
        
        # 6. Config complete
        from_radio = mesh_pb2.FromRadio()
        from_radio.config_complete_id = config_nonce
        self._send_from_radio(from_radio)
    
    def close(self):
        """Закрывает сессию и очищает ресурсы"""
        if self.mqtt_client:
            self.mqtt_client.stop()
            self.mqtt_client = None
        
        if self.client_socket:
            try:
                self.client_socket.close()
            except:
                pass
        
        info("SESSION", f"[{self._log_prefix()}] Сессия закрыта")

