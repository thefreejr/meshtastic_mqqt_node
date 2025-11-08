"""
TCP сессия для каждого подключенного клиента
"""

import socket
import time
import random
import threading
from datetime import datetime
from typing import Optional, Tuple

try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False

try:
    from meshtastic import mesh_pb2
    from meshtastic.protobuf import admin_pb2, portnums_pb2, config_pb2
    try:
        from meshtastic.protobuf import telemetry_pb2
    except ImportError:
        telemetry_pb2 = None
except ImportError:
    print("Ошибка: Установите meshtastic: pip install meshtastic")
    raise

from ..config import MAX_NUM_CHANNELS, DEFAULT_HOP_LIMIT
from ..mesh.channels import Channels
from ..mesh.node_db import NodeDB
from ..mesh.config_storage import ConfigStorage, NodeConfig
from ..mesh.persistence import Persistence
from ..mesh.rtc import RTC, RTCQuality, RTCSetResult, get_valid_time
from ..mqtt.client import MQTTClient
from ..mqtt.subscription import MQTTSubscription
from ..mesh import generate_node_id
from ..mesh.pki_manager import PKIManager
from ..mesh.settings_loader import SettingsLoader
from ..protocol.stream_api import StreamAPI
from ..protocol.packet_handler import PacketHandler
from ..protocol.admin_handler import AdminMessageHandler
from ..protocol.config_sender import ConfigSender
from ..utils.logger import info, debug, error, warn
from ..utils.exceptions import CryptoError, PersistenceError


class TCPConnectionSession:
    """Сессия для одного TCP подключения"""
    
    def __init__(self, client_socket: socket.socket, client_address: Tuple[str, int], 
                 node_id: Optional[str] = None, server=None) -> None:
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
        
        # Инициализируем телеметрию в NodeDB для нашей сессии
        self._init_telemetry()
        
        # Инициализируем позицию в NodeDB для нашей сессии
        self._init_position()
        
        # PKI ключи (Curve25519) - индивидуальные для каждой сессии
        self.pki_private_key = None
        self.pki_public_key = None
        
        # Хранилище информации о владельце (owner) - должно быть создано до логирования
        self.owner = mesh_pb2.User()
        self.owner.id = self.node_id
        self.owner.long_name = NodeConfig.USER_LONG_NAME
        self.owner.short_name = NodeConfig.USER_SHORT_NAME
        self.owner.is_licensed = False
        
        # ВАЖНО: Загружаем сохраненные настройки ПЕРЕД генерацией ключей
        # Если в сохраненных настройках есть публичный ключ, используем его
        # (как в firmware - ключи сохраняются и переиспользуются)
        settings_loader = SettingsLoader(
            persistence=self.persistence,
            channels=self.channels,
            config_storage=self.config_storage,
            owner=self.owner,
            pki_public_key=None,  # Будет установлен после загрузки
            node_id=self.node_id
        )
        settings_loader.load_all()
        
        # ВАЖНО: Если в owner уже есть публичный ключ из файла, используем его
        # Иначе генерируем новый
        if self.owner.public_key and len(self.owner.public_key) == 32:
            # Используем сохраненный публичный ключ
            self.pki_public_key = self.owner.public_key
            debug("PKI", f"[{self._log_prefix()}] Using saved public key from file: {self.pki_public_key[:8].hex()}...")
            # Приватный ключ не сохраняется (по соображениям безопасности)
            # Генерируем новый приватный ключ (но это не критично, так как мы не используем PKI расшифровку)
            self.pki_private_key = bytes(32)
        else:
            # Генерируем новые ключи только если их нет в файле
            self._generate_pki_keys()
            if self.pki_public_key and len(self.pki_public_key) == 32:
                self.owner.public_key = self.pki_public_key
                # Сохраняем owner с новым публичным ключом
                self.persistence.save_owner(self.owner)
                info("PKI", f"[{self._log_prefix()}] Generated new PKI keys and saved to file")
        
        info("SESSION", f"Session created: node_id={self.node_id}, node_num={self.node_num:08X}, address={client_address[0]}:{client_address[1]}, owner={self.owner.short_name or self.owner.long_name or 'N/A'}")
        
        # MQTT клиент будет создан при необходимости (ленивая инициализация)
        self.mqtt_client: Optional[MQTTClient] = None
        
        # Флаг для отслеживания отправки config
        self.config_sent_nodes = False
        
        # Флаг для предотвращения повторного закрытия
        self._closed = False
        
        # Throttling для публикации NodeInfo (как в firmware NodeInfoModule - не чаще чем раз в 5 минут)
        self.last_nodeinfo_published = 0  # Время последней публикации NodeInfo в MQTT
        
        # Position broadcast tracking (как в firmware PositionModule)
        self.last_position_send = 0  # Время последней отправки позиции (millis)
        self.position_seq_number = 0  # Порядковый номер позиции (инкрементируется при каждой отправке)
        self.prev_position_packet_id = 0  # ID предыдущего пакета позиции (для отмены, если нужно)
    
    def _init_telemetry(self) -> None:
        """Инициализирует телеметрию в NodeDB для этой сессии"""
        try:
            from meshtastic.protobuf import telemetry_pb2
            if telemetry_pb2:
                our_node = self.node_db.get_or_create_mesh_node(self.node_num)
                if not hasattr(our_node, 'device_metrics') or not our_node.HasField('device_metrics'):
                    device_metrics = telemetry_pb2.DeviceMetrics()
                    device_metrics.battery_level = NodeConfig.DEVICE_METRICS_BATTERY_LEVEL
                    device_metrics.voltage = NodeConfig.DEVICE_METRICS_VOLTAGE
                    device_metrics.channel_utilization = NodeConfig.DEVICE_METRICS_CHANNEL_UTILIZATION
                    device_metrics.air_util_tx = NodeConfig.DEVICE_METRICS_AIR_UTIL_TX
                    device_metrics.uptime_seconds = 0  # Будет обновляться при отправке NodeInfo
                    our_node.device_metrics.CopyFrom(device_metrics)
                    debug("SESSION", f"[{self._log_prefix()}] Initialized device_metrics in NodeDB")
        except Exception as e:
            debug("SESSION", f"[{self._log_prefix()}] Error initializing telemetry: {e}")
    
    def _init_position(self) -> None:
        """Инициализирует позицию в NodeDB для этой сессии (как в firmware)"""
        try:
            our_node = self.node_db.get_or_create_mesh_node(self.node_num)
            # Инициализируем позицию, если её нет
            if not hasattr(our_node, 'position'):
                # Создаем пустую позицию (будет установлена клиентом или через AdminMessage)
                position = mesh_pb2.Position()
                our_node.position.CopyFrom(position)
                debug("SESSION", f"[{self._log_prefix()}] Initialized position in NodeDB")
        except Exception as e:
            debug("SESSION", f"[{self._log_prefix()}] Error initializing position: {e}")
    
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
        try:
            self.pki_private_key, self.pki_public_key = PKIManager.generate_keypair()
            if self.pki_private_key is None or self.pki_public_key is None:
                # Fallback если криптография недоступна
                self.pki_private_key = bytes(32)
                self.pki_public_key = bytes(32)
                debug("PKI", f"[{self._log_prefix()}] PKI keys not generated (cryptography unavailable)")
            else:
                debug("PKI", f"[{self._log_prefix()}] Keys generated (public_key: {self.pki_public_key[:8].hex()}...)")
        except CryptoError as e:
            error("PKI", f"[{self._log_prefix()}] Error generating keys: {e}")
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
            
            # Проверяем, включен ли MQTT
            mqtt_enabled = mqtt_config.enabled if hasattr(mqtt_config, 'enabled') else True
            if not mqtt_enabled:
                debug("MQTT", f"[{self._log_prefix()}] MQTT disabled in module_config, client not created")
                return None
            
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
                        info("MQTT", f"[{self._log_prefix()}] Using MQTT settings from module_config: {broker}:{port}")
                
                if hasattr(mqtt_config, 'username') and mqtt_config.username:
                    username = mqtt_config.username.strip() or default_username
                
                if hasattr(mqtt_config, 'password') and mqtt_config.password:
                    password = mqtt_config.password.strip() or default_password
                
                if hasattr(mqtt_config, 'root') and mqtt_config.root:
                    root = mqtt_config.root.strip() or default_root
            else:
                info("MQTT", f"[{self._log_prefix()}] MQTT disabled in module_config, using default settings")
            
            self.mqtt_client = MQTTClient(
                broker=broker,
                port=port,
                username=username,
                password=password,
                root_topic=root,
                node_id=self.node_id,
                channels=self.channels,
                node_db=self.node_db,
                server=self.server  # Передаем ссылку на сервер для доступа к сессиям
            )
            
            if not self.mqtt_client.start():
                error("MQTT", f"[{self._log_prefix()}] Failed to connect to MQTT")
                return None
            
            info("MQTT", f"[{self._log_prefix()}] MQTT client created: {broker}:{port}")
            
            # Отправляем NodeInfo в MQTT при подключении (как в firmware publishNodeInfo)
            self._publish_node_info_to_mqtt()
            
            # Отправляем телеметрию в MQTT при подключении (как в firmware DeviceTelemetryModule)
            self._publish_telemetry_to_mqtt()
            
            # Отправляем позицию в MQTT при подключении (как в firmware PositionModule)
            self._send_our_position()
        
        return self.mqtt_client
    
    def _publish_node_info_to_mqtt(self) -> None:
        """Отправляет NodeInfo в MQTT (как в firmware publishNodeInfo)"""
        if not self.mqtt_client or not self.mqtt_client.connected:
            return
        
        # Throttling: не публикуем NodeInfo чаще чем раз в 5 минут (как в firmware NodeInfoModule)
        # Это предотвращает частую публикацию публичного ключа
        current_time = time.time()
        if hasattr(self, 'last_nodeinfo_published') and self.last_nodeinfo_published > 0:
            time_since_last = current_time - self.last_nodeinfo_published
            if time_since_last < 5 * 60:  # 5 минут
                debug("MQTT", f"[{self._log_prefix()}] Skipping NodeInfo publish (sent {time_since_last:.1f}s ago, <5min)")
                return
        
        try:
            import random
            from ..config import DEFAULT_HOP_LIMIT
            
            # Обновляем телеметрию в NodeDB перед отправкой (чтобы uptime_seconds был актуальным)
            try:
                from meshtastic.protobuf import telemetry_pb2
                if telemetry_pb2:
                    our_node = self.node_db.get_or_create_mesh_node(self.node_num)
                    if hasattr(our_node, 'device_metrics') and our_node.HasField('device_metrics'):
                        our_node.device_metrics.uptime_seconds = self.get_uptime_seconds()
                    else:
                        device_metrics = telemetry_pb2.DeviceMetrics()
                        device_metrics.battery_level = NodeConfig.DEVICE_METRICS_BATTERY_LEVEL
                        device_metrics.voltage = NodeConfig.DEVICE_METRICS_VOLTAGE
                        device_metrics.channel_utilization = NodeConfig.DEVICE_METRICS_CHANNEL_UTILIZATION
                        device_metrics.air_util_tx = NodeConfig.DEVICE_METRICS_AIR_UTIL_TX
                        device_metrics.uptime_seconds = self.get_uptime_seconds()
                        our_node.device_metrics.CopyFrom(device_metrics)
            except Exception as e:
                debug("MQTT", f"[{self._log_prefix()}] Error updating telemetry: {e}")
            
            # Создаем User пакет с информацией о владельце (как в firmware NodeInfoModule)
            user = mesh_pb2.User()
            user.id = self.owner.id
            user.long_name = self.owner.long_name
            user.short_name = self.owner.short_name
            user.is_licensed = self.owner.is_licensed
            
            # Публичный ключ (если не лицензирован)
            if self.owner.public_key and len(self.owner.public_key) > 0:
                if not self.owner.is_licensed:
                    user.public_key = self.owner.public_key
            
            # Создаем MeshPacket с User payload (portnum=NODEINFO_APP)
            packet = mesh_pb2.MeshPacket()
            packet.id = random.randint(1, 0xFFFFFFFF)
            packet.to = 0xFFFFFFFF  # Broadcast
            setattr(packet, 'from', self.node_num)
            packet.channel = 0  # Primary channel
            packet.decoded.portnum = portnums_pb2.PortNum.NODEINFO_APP
            packet.decoded.payload = user.SerializeToString()
            packet.hop_limit = DEFAULT_HOP_LIMIT
            packet.hop_start = DEFAULT_HOP_LIMIT
            packet.want_ack = False
            
            # Отправляем в MQTT
            self.mqtt_client.publish_packet(packet, 0)
            self.last_nodeinfo_published = current_time  # Обновляем время последней публикации
            info("MQTT", f"[{self._log_prefix()}] Published NodeInfo to MQTT: {self.owner.short_name}/{self.owner.long_name}")
        except Exception as e:
            error("MQTT", f"[{self._log_prefix()}] Error publishing NodeInfo to MQTT: {e}")
            import traceback
            traceback.print_exc()
    
    def _publish_telemetry_to_mqtt(self) -> None:
        """Отправляет телеметрию в MQTT (как в firmware DeviceTelemetryModule::sendTelemetry)"""
        if not self.mqtt_client or not self.mqtt_client.connected:
            return
        
        try:
            import random
            from ..config import DEFAULT_HOP_LIMIT
            from meshtastic.protobuf import telemetry_pb2, portnums_pb2
            
            if not telemetry_pb2:
                return
            
            # Получаем телеметрию из NodeDB
            our_node = self.node_db.get_or_create_mesh_node(self.node_num)
            if not hasattr(our_node, 'device_metrics') or not our_node.HasField('device_metrics'):
                # Инициализируем телеметрию, если её нет
                device_metrics = telemetry_pb2.DeviceMetrics()
                device_metrics.battery_level = NodeConfig.DEVICE_METRICS_BATTERY_LEVEL
                device_metrics.voltage = NodeConfig.DEVICE_METRICS_VOLTAGE
                device_metrics.channel_utilization = NodeConfig.DEVICE_METRICS_CHANNEL_UTILIZATION
                device_metrics.air_util_tx = NodeConfig.DEVICE_METRICS_AIR_UTIL_TX
                device_metrics.uptime_seconds = self.get_uptime_seconds()
                our_node.device_metrics.CopyFrom(device_metrics)
            
            # Обновляем uptime_seconds
            our_node.device_metrics.uptime_seconds = self.get_uptime_seconds()
            
            # Создаем Telemetry пакет (как в firmware)
            telemetry = telemetry_pb2.Telemetry()
            telemetry.time = int(time.time())
            # В protobuf Python для oneof полей используем прямое присваивание
            telemetry.device_metrics.CopyFrom(our_node.device_metrics)
            
            # Создаем MeshPacket с Telemetry payload (portnum=TELEMETRY_APP)
            packet = mesh_pb2.MeshPacket()
            packet.id = random.randint(1, 0xFFFFFFFF)
            packet.to = 0xFFFFFFFF  # Broadcast
            setattr(packet, 'from', self.node_num)
            packet.channel = 0  # Primary channel
            packet.decoded.portnum = portnums_pb2.PortNum.TELEMETRY_APP
            packet.decoded.payload = telemetry.SerializeToString()
            packet.hop_limit = DEFAULT_HOP_LIMIT
            packet.hop_start = DEFAULT_HOP_LIMIT
            packet.want_ack = False
            
            # Отправляем в MQTT
            self.mqtt_client.publish_packet(packet, 0)
            info("MQTT", f"[{self._log_prefix()}] Published Telemetry to MQTT: battery={our_node.device_metrics.battery_level}, uptime={our_node.device_metrics.uptime_seconds}")
        except Exception as e:
            error("MQTT", f"[{self._log_prefix()}] Error publishing Telemetry to MQTT: {e}")
            import traceback
            traceback.print_exc()
    
    def get_uptime_seconds(self) -> int:
        """Возвращает uptime в секундах для этой сессии"""
        return int(time.time() - self.created_at)
    
    def _alloc_position_packet(self, channel_index: int = 0) -> Optional[mesh_pb2.MeshPacket]:
        """
        Создает пакет с позицией (как в firmware PositionModule::allocPositionPacket)
        
        Args:
            channel_index: Индекс канала для определения precision
            
        Returns:
            MeshPacket с позицией или None если позиция невалидна
        """
        try:
            # Получаем precision из настроек канала (как в firmware)
            precision = 32  # По умолчанию полная точность
            ch = self.channels.get_by_index(channel_index)
            if hasattr(ch.settings, 'module_settings') and ch.settings.HasField('module_settings'):
                if hasattr(ch.settings.module_settings, 'position_precision'):
                    precision = ch.settings.module_settings.position_precision
            
            # Если precision = 0, не отправляем позицию (как в firmware)
            if precision == 0:
                debug("POSITION", f"[{self._log_prefix()}] Skip location send because precision is set to 0!")
                return None
            
            # Получаем позицию из NodeDB
            our_node = self.node_db.get_or_create_mesh_node(self.node_num)
            if not hasattr(our_node, 'position') or not our_node.HasField('position'):
                debug("POSITION", f"[{self._log_prefix()}] No position in NodeDB")
                return None
            
            position = our_node.position
            
            # Проверяем, что координаты не нулевые (как в firmware)
            if not hasattr(position, 'latitude_i') or not hasattr(position, 'longitude_i'):
                debug("POSITION", f"[{self._log_prefix()}] Position missing latitude_i or longitude_i")
                return None
            
            if position.latitude_i == 0 and position.longitude_i == 0:
                debug("POSITION", f"[{self._log_prefix()}] Skip position send because lat/lon are zero!")
                return None
            
            # Получаем position_flags из конфигурации
            pos_flags = self.config_storage.config.position.position_flags
            
            # Создаем Position пакет (как в firmware PositionModule::allocPositionPacket)
            p = mesh_pb2.Position()
            
            # lat/lon всегда включаются (с учетом precision)
            if precision < 32 and precision > 0:
                # Обрезаем координаты до указанной точности (как в firmware)
                mask = (0xFFFFFFFF << (32 - precision)) & 0xFFFFFFFF
                p.latitude_i = position.latitude_i & mask
                p.longitude_i = position.longitude_i & mask
                # Сдвигаем к центру возможной области (как в firmware)
                p.latitude_i += (1 << (31 - precision))
                p.longitude_i += (1 << (31 - precision))
            else:
                p.latitude_i = position.latitude_i
                p.longitude_i = position.longitude_i
            
            p.precision_bits = precision
            p.has_latitude_i = True
            p.has_longitude_i = True
            
            # Время (всегда включается, если доступно)
            from ..mesh.rtc import RTCQuality, get_valid_time
            time_value = get_valid_time(RTCQuality.NTP)
            if time_value > 0:
                p.time = time_value
            elif self.rtc.get_quality() >= RTCQuality.DEVICE:
                p.time = self.rtc.get_valid_time(RTCQuality.DEVICE)
            else:
                p.time = 0
            
            # Источник местоположения
            if self.config_storage.config.position.fixed_position:
                p.location_source = mesh_pb2.Position.LocSource.LOC_MANUAL
            elif hasattr(position, 'location_source'):
                p.location_source = position.location_source
            
            # Условные поля на основе position_flags (как в firmware)
            if pos_flags & 0x0001:  # ALTITUDE
                if pos_flags & 0x0002:  # ALTITUDE_MSL
                    if hasattr(position, 'altitude'):
                        p.altitude = position.altitude
                        p.has_altitude = True
                else:
                    if hasattr(position, 'altitude_hae'):
                        p.altitude_hae = position.altitude_hae
                        p.has_altitude_hae = True
                
                if pos_flags & 0x0004:  # GEOIDAL_SEPARATION
                    if hasattr(position, 'altitude_geoidal_separation'):
                        p.altitude_geoidal_separation = position.altitude_geoidal_separation
                        p.has_altitude_geoidal_separation = True
            
            if pos_flags & 0x0008:  # DOP
                if pos_flags & 0x0010:  # HVDOP
                    if hasattr(position, 'HDOP'):
                        p.HDOP = position.HDOP
                    if hasattr(position, 'VDOP'):
                        p.VDOP = position.VDOP
                else:
                    if hasattr(position, 'PDOP'):
                        p.PDOP = position.PDOP
            
            if pos_flags & 0x0020:  # SATINVIEW
                if hasattr(position, 'sats_in_view'):
                    p.sats_in_view = position.sats_in_view
            
            if pos_flags & 0x0080:  # TIMESTAMP
                if hasattr(position, 'timestamp'):
                    p.timestamp = position.timestamp
            
            if pos_flags & 0x0040:  # SEQ_NO
                self.position_seq_number += 1
                p.seq_number = self.position_seq_number
            
            if pos_flags & 0x0100:  # HEADING
                if hasattr(position, 'ground_track'):
                    p.ground_track = position.ground_track
                    p.has_ground_track = True
            
            if pos_flags & 0x0200:  # SPEED
                if hasattr(position, 'ground_speed'):
                    p.ground_speed = position.ground_speed
                    p.has_ground_speed = True
            
            # Создаем MeshPacket
            import random
            from ..config import DEFAULT_HOP_LIMIT
            
            packet = mesh_pb2.MeshPacket()
            packet.id = random.randint(1, 0xFFFFFFFF)
            packet.to = 0xFFFFFFFF  # Broadcast
            setattr(packet, 'from', self.node_num)
            packet.channel = channel_index
            packet.decoded.portnum = portnums_pb2.PortNum.POSITION_APP
            packet.decoded.payload = p.SerializeToString()
            packet.hop_limit = DEFAULT_HOP_LIMIT
            packet.hop_start = DEFAULT_HOP_LIMIT
            packet.want_ack = False
            
            # Определяем приоритет (как в firmware)
            device_role = self.config_storage.config.device.role
            if device_role == config_pb2.Config.DeviceConfig.Role.TRACKER or \
               device_role == config_pb2.Config.DeviceConfig.Role.TAK_TRACKER:
                packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
            else:
                packet.priority = mesh_pb2.MeshPacket.Priority.BACKGROUND
            
            # Определяем want_response (как в firmware)
            if device_role == config_pb2.Config.DeviceConfig.Role.TRACKER:
                packet.decoded.want_response = False
            else:
                # Для других ролей можно запрашивать ответы при смене поколения радио
                # Пока упростим: не запрашиваем ответы
                packet.decoded.want_response = False
            
            debug("POSITION", f"[{self._log_prefix()}] Position packet created: time={p.time}, lat={p.latitude_i}, lon={p.longitude_i}, precision={precision}")
            return packet
            
        except Exception as e:
            error("POSITION", f"[{self._log_prefix()}] Error creating position packet: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _send_our_position(self, channel_index: int = 0) -> None:
        """
        Отправляет нашу позицию в MQTT (как в firmware PositionModule::sendOurPosition)
        
        Args:
            channel_index: Индекс канала для отправки
        """
        if not self.mqtt_client or not self.mqtt_client.connected:
            debug("POSITION", f"[{self._log_prefix()}] MQTT not connected, skipping position send")
            return
        
        try:
            # Ищем канал с включенной отправкой позиции (как в firmware)
            # Перебираем каналы 0-7 и ищем первый с position_precision != 0
            target_channel = None
            for ch_num in range(MAX_NUM_CHANNELS):
                ch = self.channels.get_by_index(ch_num)
                if hasattr(ch.settings, 'module_settings') and ch.settings.HasField('module_settings'):
                    if hasattr(ch.settings.module_settings, 'position_precision'):
                        if ch.settings.module_settings.position_precision != 0:
                            target_channel = ch_num
                            break
            
            if target_channel is None:
                # Если не нашли канал с включенной позицией, используем PRIMARY канал
                target_channel = self.channels._get_primary_index()
            
            # Создаем пакет позиции
            packet = self._alloc_position_packet(target_channel)
            if packet is None:
                debug("POSITION", f"[{self._log_prefix()}] Failed to create position packet")
                return
            
            # Отменяем предыдущий неотправленный пакет позиции (если есть, как в firmware)
            if self.prev_position_packet_id:
                # В MQTT нет механизма отмены, но сохраняем ID для логирования
                debug("POSITION", f"[{self._log_prefix()}] Previous position packet ID: {self.prev_position_packet_id}")
            
            self.prev_position_packet_id = packet.id
            
            # Отправляем в MQTT
            self.mqtt_client.publish_packet(packet, target_channel)
            self.last_position_send = int(time.time() * 1000)  # В миллисекундах
            
            info("POSITION", f"[{self._log_prefix()}] Published Position to MQTT: lat={packet.decoded.payload[:4] if len(packet.decoded.payload) >= 4 else 'N/A'}, channel={target_channel}")
            
        except Exception as e:
            error("POSITION", f"[{self._log_prefix()}] Error sending position: {e}")
            import traceback
            traceback.print_exc()
    
    def _run_position_broadcast(self) -> None:
        """
        Периодическая отправка позиции (как в firmware PositionModule::runOnce)
        Вызывается периодически для проверки необходимости отправки позиции
        """
        if not self.mqtt_client or not self.mqtt_client.connected:
            return
        
        try:
            # Получаем интервал отправки из конфигурации (как в firmware)
            broadcast_secs = self.config_storage.config.position.position_broadcast_secs
            if broadcast_secs == 0:
                broadcast_secs = 15 * 60  # Дефолт: 15 минут
            
            # Масштабируем интервал в зависимости от количества узлов (как в firmware)
            # Упрощенная версия: просто используем базовый интервал
            interval_ms = broadcast_secs * 1000
            
            now_ms = int(time.time() * 1000)
            ms_since_last_send = now_ms - self.last_position_send
            
            # Проверяем, прошло ли достаточно времени (как в firmware)
            if self.last_position_send == 0 or ms_since_last_send >= interval_ms:
                # Проверяем, есть ли валидная позиция
                our_node = self.node_db.get_or_create_mesh_node(self.node_num)
                if hasattr(our_node, 'position') and our_node.HasField('position'):
                    position = our_node.position
                    if hasattr(position, 'latitude_i') and hasattr(position, 'longitude_i'):
                        if position.latitude_i != 0 or position.longitude_i != 0:
                            # Отправляем позицию
                            self._send_our_position()
                            debug("POSITION", f"[{self._log_prefix()}] Position broadcast sent (interval={broadcast_secs}s)")
            
        except Exception as e:
            debug("POSITION", f"[{self._log_prefix()}] Error in position broadcast: {e}")
    
    def _send_from_radio(self, from_radio: mesh_pb2.FromRadio) -> None:
        """Отправляет FromRadio сообщение клиенту через TCP сокет"""
        try:
            payload = from_radio.SerializeToString()
            framed = StreamAPI.add_framing(payload)
            
            # Определяем тип сообщения для логирования
            msg_type = from_radio.WhichOneof('payload_variant')
            if msg_type:
                debug("TCP", f"[{self._log_prefix()}] Sending FromRadio: {msg_type} (size: {len(payload)} bytes)")
            
            sent = self.client_socket.send(framed)
            if sent != len(framed):
                warn("TCP", f"[{self._log_prefix()}] Sent only {sent} of {len(framed)} bytes")
        except Exception as e:
            error("TCP", f"[{self._log_prefix()}] Error sending FromRadio: {e}")
            import traceback
            traceback.print_exc()
    
    def _load_settings(self):
        """Загружает сохраненные настройки при запуске"""
        try:
            # Используем SettingsLoader для загрузки всех настроек
            settings_loader = SettingsLoader(
                persistence=self.persistence,
                channels=self.channels,
                config_storage=self.config_storage,
                owner=self.owner,
                pki_public_key=self.pki_public_key,
                node_id=self.node_id
            )
            
            loaded_count = settings_loader.load_all()
            
            # Загружаем шаблонные сообщения отдельно (нужно сохранить в self.canned_messages)
            saved_canned_messages = self.persistence.load_canned_messages()
            if saved_canned_messages is not None:
                self.canned_messages = saved_canned_messages
            
            # ВАЖНО: MQTT клиент будет создан ПОСЛЕ загрузки настроек (в server.py),
            # поэтому здесь мы просто загружаем настройки, а клиент будет создан с правильными настройками
            # Если клиент уже создан (например, при повторной загрузке после обновления node_id),
            # применяем загруженные настройки
            if self.mqtt_client and self.config_storage.module_config.mqtt.enabled:
                # Применяем настройки MQTT из загруженного module_config
                info("PERSISTENCE", f"[{self._log_prefix()}] Applying loaded MQTT settings to existing client...")
                mqtt_config = self.config_storage.module_config.mqtt
                self.mqtt_client.update_config(mqtt_config)
                
                # После обновления настроек MQTT нужно переподписаться на топики
                if self.mqtt_client.connected:
                    info("PERSISTENCE", f"[{self._log_prefix()}] Resubscribing to MQTT topics after settings update...")
                    self.mqtt_client._send_subscriptions(self.mqtt_client.client)
            
            # ВАЖНО: После загрузки каналов нужно переподписаться на MQTT топики,
            # если клиент уже создан (например, при повторной загрузке)
            if self.mqtt_client and self.mqtt_client.connected:
                info("PERSISTENCE", f"[{self._log_prefix()}] Resubscribing to MQTT topics after loading channels...")
                # Проверяем Custom канал перед переподпиской
                custom_ch = self.channels.get_by_index(1)
                info("PERSISTENCE", f"[{self._log_prefix()}] Custom channel before resubscription: downlink_enabled={custom_ch.settings.downlink_enabled}")
                self.mqtt_client._send_subscriptions(self.mqtt_client.client)
            
        except PersistenceError as e:
            error("PERSISTENCE", f"[{self._log_prefix()}] Error loading settings: {e}")
            warn("PERSISTENCE", f"[{self._log_prefix()}] Continuing with default settings")
        except Exception as e:
            error("PERSISTENCE", f"[{self._log_prefix()}] Unexpected error loading settings: {e}")
            import traceback
            traceback.print_exc()
            warn("PERSISTENCE", f"[{self._log_prefix()}] Continuing with default settings")
    
    def _handle_to_radio(self, payload: bytes) -> None:
        """Обрабатывает ToRadio сообщение"""
        try:
            to_radio = mesh_pb2.ToRadio()
            to_radio.ParseFromString(payload)
            
            msg_type = to_radio.WhichOneof('payload_variant')
            debug("TCP", f"[{self._log_prefix()}] Received ToRadio: {msg_type}")
            
            if to_radio.HasField('want_config_id'):
                debug("TCP", f"[{self._log_prefix()}] Configuration request with want_config_id={to_radio.want_config_id}")
                self._send_config(to_radio.want_config_id)
            elif to_radio.HasField('packet'):
                debug("TCP", f"[{self._log_prefix()}] ToRadio contains MeshPacket")
                self._handle_mesh_packet(to_radio.packet)
        except Exception as e:
            error("TCP", f"[{self._log_prefix()}] Error processing ToRadio: {e}")
            import traceback
            traceback.print_exc()
    
    def _handle_mesh_packet(self, packet: mesh_pb2.MeshPacket) -> None:
        """Обрабатывает MeshPacket"""
        try:
            # Используем PacketHandler для подготовки пакета
            PacketHandler.prepare_outgoing_packet(packet)
            
            payload_type = packet.WhichOneof('payload_variant')
            packet_from = getattr(packet, 'from', 0)
            hop_limit = getattr(packet, 'hop_limit', 0)
            hop_start = getattr(packet, 'hop_start', 0)
            want_ack = getattr(packet, 'want_ack', False)
            
            # Используем PacketHandler для вычисления hops_away
            hops_away = PacketHandler.get_hops_away(packet)
            if hops_away > 0:
                debug("TCP", f"[{self._log_prefix()}] Route trace: hops_away={hops_away}, hop_start={hop_start}, hop_limit={hop_limit}")
            
            packet_to = packet.to
            debug("TCP", f"[{self._log_prefix()}] Received MeshPacket: payload_variant={payload_type}, id={packet.id}, from={packet_from:08X}, to={packet_to:08X}, channel={packet.channel}, want_ack={want_ack}, hop_limit={hop_limit}, hop_start={hop_start}, hops_away={hops_away}")
            
            # Используем PacketHandler для проверки Admin пакета
            if PacketHandler.is_admin_packet(packet):
                debug("TCP", f"[{self._log_prefix()}] AdminMessage detected, forwarding to handler (want_response={getattr(packet.decoded, 'want_response', False)})")
                self._handle_admin_message(packet)
            
            # Устанавливаем поле from на наш node_num перед отправкой в MQTT
            # (как в firmware Router.cpp: p->from = getFrom(p))
            # ВАЖНО: Пакеты от клиента всегда имеют from=0, устанавливаем на наш node_num
            if packet_from == 0:
                setattr(packet, 'from', self.node_num)
                info("TCP", f"[{self._log_prefix()}] Set packet.from={self.node_num:08X} (was 0) before MQTT publish")
            else:
                # Если from уже установлен, это может быть пересылаемый пакет
                # Но для пакетов от клиента через TCP это не должно происходить
                warn("TCP", f"[{self._log_prefix()}] Packet already has from={packet_from:08X}, not setting to {self.node_num:08X}")
            
            # Проверяем значение перед отправкой
            final_from = getattr(packet, 'from', 0)
            debug("TCP", f"[{self._log_prefix()}] Packet before MQTT publish: from={final_from:08X}, to={packet.to:08X}, id={packet.id}")
            
            channel_index = packet.channel if packet.channel < MAX_NUM_CHANNELS else 0
            
            # Обработка пакетов позиции от клиента (как в firmware PositionModule::handleReceivedProtobuf)
            if hasattr(packet.decoded, 'portnum') and packet.decoded.portnum == portnums_pb2.PortNum.POSITION_APP:
                try:
                    position = mesh_pb2.Position()
                    position.ParseFromString(packet.decoded.payload)
                    # Обновляем позицию в NodeDB (как в firmware nodeDB->updatePosition)
                    self.node_db.update_position(self.node_num, position)
                    debug("POSITION", f"[{self._log_prefix()}] Updated position from client: lat={position.latitude_i}, lon={position.longitude_i}")
                except Exception as e:
                    debug("POSITION", f"[{self._log_prefix()}] Error parsing position from client: {e}")
            
            if self.mqtt_client:
                self.mqtt_client.publish_packet(packet, channel_index)
            
            # Обработка запросов позиции (как в firmware PositionModule::allocReply)
            if hasattr(packet.decoded, 'portnum') and packet.decoded.portnum == portnums_pb2.PortNum.POSITION_APP:
                if hasattr(packet.decoded, 'want_response') and packet.decoded.want_response:
                    # Клиент запрашивает позицию - отправляем ответ (как в firmware allocReply)
                    debug("POSITION", f"[{self._log_prefix()}] Position request received, sending reply")
                    reply_packet = self._alloc_position_packet(packet.channel if packet.channel < MAX_NUM_CHANNELS else 0)
                    if reply_packet:
                        # Настраиваем пакет как ответ (как в firmware)
                        reply_packet.to = packet_from if packet_from != 0 else 0xFFFFFFFF
                        reply_packet.decoded.want_response = False
                        reply_packet.decoded.request_id = packet.id
                        
                        # Отправляем ответ клиенту через TCP
                        from_radio = mesh_pb2.FromRadio()
                        from_radio.packet.CopyFrom(reply_packet)
                        self._send_from_radio(from_radio)
                        info("POSITION", f"[{self._log_prefix()}] Sent position reply (request_id={packet.id})")
            
            # Используем PacketHandler для проверки необходимости отправки ACK
            if PacketHandler.should_send_ack(packet):
                portnum_name = packet.decoded.portnum if hasattr(packet.decoded, 'portnum') else 'N/A'
                debug("ACK", f"[{self._log_prefix()}] Sending ACK for packet {packet.id} (portnum={portnum_name}, from={packet_from:08X})")
                self._send_ack(packet, channel_index)
        except Exception as e:
            error("TCP", f"[{self._log_prefix()}] Error processing MeshPacket: {e}")
            import traceback
            traceback.print_exc()
    
    def _handle_mqtt_packet(self, packet: mesh_pb2.MeshPacket) -> None:
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
                        error("NODE", f"[{self._log_prefix()}] Error updating user info: {e}")
                
                elif packet.decoded.portnum == portnums_pb2.PortNum.TELEMETRY_APP:
                    try:
                        if telemetry_pb2:
                            telemetry = telemetry_pb2.Telemetry()
                            telemetry.ParseFromString(packet.decoded.payload)
                            variant = telemetry.WhichOneof('variant')
                            if variant == 'device_metrics':
                                self.node_db.update_telemetry(packet_from, telemetry.device_metrics)
                    except Exception as e:
                        error("NODE", f"[{self._log_prefix()}] Error updating telemetry: {e}")
                        import traceback
                        traceback.print_exc()
                
                elif packet.decoded.portnum == portnums_pb2.PortNum.POSITION_APP:
                    try:
                        position = mesh_pb2.Position()
                        position.ParseFromString(packet.decoded.payload)
                        self.node_db.update_position(packet_from, position)
                    except Exception as e:
                        error("NODE", f"[{self._log_prefix()}] Error updating position: {e}")
        except Exception as e:
            error("MQTT", f"[{self._log_prefix()}] Error processing MeshPacket from MQTT: {e}")
            import traceback
            traceback.print_exc()
    
    def _send_ack(self, packet: mesh_pb2.MeshPacket, channel_index: int) -> None:
        """Отправляет ACK пакет обратно клиенту"""
        try:
            # Используем PacketHandler для создания ACK пакета
            ack_packet = PacketHandler.create_ack_packet(packet, self.node_num, channel_index)
            
            def send_ack_delayed():
                time.sleep(0.1)  # 100ms задержка
                try:
                    from_radio = mesh_pb2.FromRadio()
                    from_radio.packet.CopyFrom(ack_packet)
                    self._send_from_radio(from_radio)
                    packet_from = getattr(packet, 'from', 0)
                    packet_to = packet.to
                    is_broadcast = packet_to == 0xFFFFFFFF
                    debug("ACK", f"[{self._log_prefix()}] Sent ACK (async): packet_id={ack_packet.id}, request_id={packet.id}, to={packet_from:08X}, from={self.node_num:08X}, channel={channel_index}, packet_to={packet_to:08X}, broadcast={is_broadcast}, error_reason=NONE")
                except Exception as e:
                    error("ACK", f"[{self._log_prefix()}] Error sending ACK (async): {e}")
            
            import threading
            ack_thread = threading.Thread(target=send_ack_delayed, daemon=True)
            ack_thread.start()
            
            debug("ACK", f"[{self._log_prefix()}] Started async ACK send for packet {packet.id} (delay 100ms)")
        except Exception as e:
            error("ACK", f"[{self._log_prefix()}] Error sending ACK: {e}")
            import traceback
            traceback.print_exc()
    
    def _handle_admin_message(self, packet: mesh_pb2.MeshPacket) -> None:
        """Обрабатывает AdminMessage из MeshPacket"""
        try:
            admin_msg = admin_pb2.AdminMessage()
            admin_msg.ParseFromString(packet.decoded.payload)
            
            msg_type = admin_msg.WhichOneof('payload_variant')
            want_response = getattr(packet.decoded, 'want_response', False)
            packet_id = packet.id
            packet_from = getattr(packet, 'from', 0)
            info("ADMIN", f"[{self._log_prefix()}] Received request: {msg_type} (packet_id={packet_id}, from={packet_from:08X}, want_response={want_response})")
            
            if admin_msg.HasField('set_owner'):
                owner_data = admin_msg.set_owner
                info("ADMIN", f"[{self._log_prefix()}] Setting owner: long_name='{owner_data.long_name}', short_name='{owner_data.short_name}'")
                
                if owner_data.long_name:
                    self.owner.long_name = owner_data.long_name
                if owner_data.short_name:
                    self.owner.short_name = owner_data.short_name
                self.owner.is_licensed = owner_data.is_licensed
                if hasattr(owner_data, 'is_unmessagable'):
                    self.owner.is_unmessagable = owner_data.is_unmessagable
                
                self.owner.id = self.node_id
                self.persistence.save_owner(self.owner)
                debug("ADMIN", f"[{self._log_prefix()}] Owner updated: {self.owner.long_name}/{self.owner.short_name}")
            
            elif admin_msg.HasField('set_channel'):
                self.channels.set_channel(admin_msg.set_channel)
                self.persistence.save_channels(self.channels.channels)
                ch_index = admin_msg.set_channel.index
                ch = self.channels.get_by_index(ch_index)
                channel_id = self.channels.get_global_id(ch_index)
                info("ADMIN", f"[{self._log_prefix()}] Channel {ch_index} set: role={ch.role}, name={ch.settings.name if ch.settings.name else 'N/A'}, downlink_enabled={ch.settings.downlink_enabled}, global_id={channel_id}")
                
                # ВАЖНО: После изменения канала нужно переподписаться на MQTT топики
                # (как в firmware MQTT::sendSubscriptions вызывается после изменения каналов)
                if self.mqtt_client and self.mqtt_client.connected and self.mqtt_client.client:
                    info("MQTT", f"[{self._log_prefix()}] Resubscribing to MQTT topics after channel {ch_index} update...")
                    # Обновляем subscription с новыми каналами
                    self.mqtt_client.subscription = MQTTSubscription(self.mqtt_client.root_topic, self.channels, self.mqtt_client.node_id)
                    # Переподписываемся на все каналы
                    self.mqtt_client.subscription.subscribe_to_channels(self.mqtt_client.client)
            
            elif admin_msg.HasField('set_config'):
                config_type = admin_msg.set_config.WhichOneof('payload_variant')
                info("ADMIN", f"[{self._log_prefix()}] Configuration set: {config_type}")
                self.config_storage.set_config(admin_msg.set_config)
                self.persistence.save_config(self.config_storage.config)
                debug("ADMIN", f"[{self._log_prefix()}] Configuration saved to ConfigStorage")
            
            elif admin_msg.HasField('set_module_config'):
                module_type = admin_msg.set_module_config.WhichOneof('payload_variant')
                info("ADMIN", f"[{self._log_prefix()}] Module configuration set: {module_type}")
                self.config_storage.set_module_config(admin_msg.set_module_config)
                self.persistence.save_module_config(self.config_storage.module_config)
                debug("ADMIN", f"[{self._log_prefix()}] Module configuration saved to ConfigStorage")
                
                if module_type == 'mqtt':
                    mqtt_config = admin_msg.set_module_config.mqtt
                    mqtt_enabled = mqtt_config.enabled if hasattr(mqtt_config, 'enabled') else True
                    
                    if mqtt_enabled:
                        # MQTT включен - создаем или обновляем клиент
                        if self.mqtt_client:
                            info("MQTT", f"[{self._log_prefix()}] MQTT enabled, updating settings...")
                            self.mqtt_client.update_config(mqtt_config)
                        else:
                            # Создаем MQTT клиент если его еще нет
                            info("MQTT", f"[{self._log_prefix()}] MQTT enabled, creating client...")
                            from ..tcp.server import TCPServer
                            if self.server:
                                self.mqtt_client = self.get_or_create_mqtt_client(
                                    default_broker=self.server.default_mqtt_broker,
                                    default_port=self.server.default_mqtt_port,
                                    default_username=self.server.default_mqtt_username,
                                    default_password=self.server.default_mqtt_password,
                                    default_root=self.server.default_mqtt_root
                                )
                            else:
                                warn("MQTT", f"[{self._log_prefix()}] Failed to create MQTT client: server not set")
                    else:
                        # MQTT выключен - останавливаем клиент
                        if self.mqtt_client:
                            info("MQTT", f"[{self._log_prefix()}] MQTT disabled, stopping client...")
                            self.mqtt_client.stop()
                            self.mqtt_client = None
                        else:
                            debug("MQTT", f"[{self._log_prefix()}] MQTT disabled, client already does not exist")
            
            elif admin_msg.HasField('get_channel_request'):
                requested_index = admin_msg.get_channel_request
                ch_index = requested_index - 1
                
                debug("ADMIN", f"[{self._log_prefix()}] get_channel_request: {requested_index} (channel index: {ch_index})")
                
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", f"[{self._log_prefix()}] get_channel_request without want_response (channel {ch_index})")
                    return
                
                if 0 <= ch_index < MAX_NUM_CHANNELS:
                    ch = self.channels.get_by_index(ch_index)
                    debug("ADMIN", f"[{self._log_prefix()}] Channel {ch_index}: role={ch.role}, name={ch.settings.name if ch.settings.name else 'N/A'}, index={ch.index}")
                    
                    admin_response = admin_pb2.AdminMessage()
                    admin_response.get_channel_response.CopyFrom(ch)
                    if admin_response.get_channel_response.index != ch_index:
                        admin_response.get_channel_response.index = ch_index
                        debug("ADMIN", f"[{self._log_prefix()}] Fixed channel index: {admin_response.get_channel_response.index}")
                    
                    # Используем AdminMessageHandler для создания reply пакета
                    reply_packet = AdminMessageHandler.create_reply_packet(packet, admin_response, self.node_num)
                    
                    from_radio = mesh_pb2.FromRadio()
                    from_radio.packet.CopyFrom(reply_packet)
                    self._send_from_radio(from_radio)
                    info("ADMIN", f"[{self._log_prefix()}] Sent get_channel_response for channel {ch_index} (request_id={packet.id})")
                else:
                    warn("ADMIN", f"[{self._log_prefix()}] Invalid channel index: {ch_index} (requested: {requested_index}, max: {MAX_NUM_CHANNELS-1})")
            
            elif admin_msg.HasField('get_config_request'):
                config_type = admin_msg.get_config_request
                debug("ADMIN", f"[{self._log_prefix()}] get_config_request: {config_type}")
                
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", f"[{self._log_prefix()}] get_config_request without want_response (config_type={config_type})")
                    return
                
                config_response = self.config_storage.get_config(config_type)
                if config_response is None:
                    warn("ADMIN", f"[{self._log_prefix()}] Unknown configuration type: {config_type}")
                    return
                
                admin_response = admin_pb2.AdminMessage()
                admin_response.get_config_response.CopyFrom(config_response)
                
                # Используем AdminMessageHandler для создания reply пакета
                reply_packet = AdminMessageHandler.create_reply_packet(packet, admin_response, self.node_num)
                
                from_radio = mesh_pb2.FromRadio()
                from_radio.packet.CopyFrom(reply_packet)
                self._send_from_radio(from_radio)
                
                try:
                    config_type_name = next((name for name, value in admin_pb2.AdminMessage.ConfigType.__dict__.items() 
                                           if isinstance(value, int) and value == config_type), f"TYPE_{config_type}")
                except:
                    config_type_name = str(config_type)
                info("ADMIN", f"[{self._log_prefix()}] Sent get_config_response for {config_type_name} (request_id={packet.id})")
            
            elif admin_msg.HasField('get_module_config_request'):
                module_config_type = admin_msg.get_module_config_request
                debug("ADMIN", f"[{self._log_prefix()}] get_module_config_request: {module_config_type}")
                
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", f"[{self._log_prefix()}] get_module_config_request without want_response (module_config_type={module_config_type})")
                    return
                
                module_config_response = self.config_storage.get_module_config(module_config_type)
                if module_config_response is None:
                    warn("ADMIN", f"[{self._log_prefix()}] Unknown module configuration type: {module_config_type}")
                    return
                
                admin_response = admin_pb2.AdminMessage()
                admin_response.get_module_config_response.CopyFrom(module_config_response)
                
                # Используем AdminMessageHandler для создания reply пакета
                reply_packet = AdminMessageHandler.create_reply_packet(packet, admin_response, self.node_num)
                
                from_radio = mesh_pb2.FromRadio()
                from_radio.packet.CopyFrom(reply_packet)
                self._send_from_radio(from_radio)
                
                try:
                    module_config_type_name = next((name for name, value in admin_pb2.AdminMessage.ModuleConfigType.__dict__.items() 
                                                   if isinstance(value, int) and value == module_config_type), f"TYPE_{module_config_type}")
                except:
                    module_config_type_name = str(module_config_type)
                info("ADMIN", f"[{self._log_prefix()}] Sent get_module_config_response for {module_config_type_name} (request_id={packet.id})")
            
            elif admin_msg.HasField('get_canned_message_module_messages_request'):
                debug("ADMIN", f"[{self._log_prefix()}] get_canned_message_module_messages_request")
                
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", f"[{self._log_prefix()}] get_canned_message_module_messages_request without want_response")
                    return
                
                messages = self.canned_messages
                
                admin_response = admin_pb2.AdminMessage()
                admin_response.get_canned_message_module_messages_response = messages
                
                # Используем AdminMessageHandler для создания reply пакета
                reply_packet = AdminMessageHandler.create_reply_packet(packet, admin_response, self.node_num)
                
                from_radio = mesh_pb2.FromRadio()
                from_radio.packet.CopyFrom(reply_packet)
                self._send_from_radio(from_radio)
                info("ADMIN", f"[{self._log_prefix()}] Sent get_canned_message_module_messages_response (request_id={packet.id})")
            
            elif admin_msg.HasField('set_canned_message_module_messages'):
                messages = admin_msg.set_canned_message_module_messages
                info("ADMIN", f"[{self._log_prefix()}] Setting canned messages: '{messages[:50]}...' (length: {len(messages)})")
                
                self.canned_messages = messages
                if messages:
                    self.config_storage.module_config.canned_message.enabled = True
                
                self.persistence.save_canned_messages(self.canned_messages)
                self.persistence.save_module_config(self.config_storage.module_config)
                debug("ADMIN", f"[{self._log_prefix()}] Canned messages saved")
            
            elif admin_msg.HasField('get_owner_request'):
                debug("ADMIN", f"[{self._log_prefix()}] get_owner_request")
                
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", f"[{self._log_prefix()}] get_owner_request without want_response")
                    return
                
                admin_response = admin_pb2.AdminMessage()
                admin_response.get_owner_response.CopyFrom(self.owner)
                
                # Используем AdminMessageHandler для создания reply пакета
                reply_packet = AdminMessageHandler.create_reply_packet(packet, admin_response, self.node_num)
                
                from_radio = mesh_pb2.FromRadio()
                from_radio.packet.CopyFrom(reply_packet)
                self._send_from_radio(from_radio)
                info("ADMIN", f"[{self._log_prefix()}] Sent get_owner_response (request_id={packet.id})")
            
            elif admin_msg.HasField('get_device_metadata_request'):
                debug("ADMIN", f"[{self._log_prefix()}] get_device_metadata_request")
                
                if not getattr(packet.decoded, 'want_response', False):
                    warn("ADMIN", f"[{self._log_prefix()}] get_device_metadata_request without want_response")
                    return
                
                device_metadata = mesh_pb2.DeviceMetadata()
                # ВАЖНО: Убеждаемся, что версия прошивки всегда установлена и не пустая
                # (клиент проверяет версию и требует обновление, если версия меньше минимальной или пустая)
                firmware_version = NodeConfig.FIRMWARE_VERSION
                if not firmware_version or firmware_version.strip() == "":
                    # Если версия не установлена, используем дефолтную (достаточно новую)
                    firmware_version = "2.6.11"
                    warn("ADMIN", f"[{self._log_prefix()}] Firmware version not set, using default: {firmware_version}")
                device_metadata.firmware_version = firmware_version
                debug("ADMIN", f"[{self._log_prefix()}] Sending DeviceMetadata with firmware_version: {firmware_version}")
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
                    debug("ADMIN", f"[{self._log_prefix()}] Failed to set hw_model in DeviceMetadata: {e}")
                device_metadata.hasRemoteHardware = self.config_storage.module_config.remote_hardware.enabled
                device_metadata.hasPKC = CRYPTOGRAPHY_AVAILABLE
                device_metadata.excluded_modules = mesh_pb2.ExcludedModules.EXCLUDED_NONE
                
                admin_response = admin_pb2.AdminMessage()
                admin_response.get_device_metadata_response.CopyFrom(device_metadata)
                
                # Используем AdminMessageHandler для создания reply пакета
                reply_packet = AdminMessageHandler.create_reply_packet(packet, admin_response, self.node_num)
                
                from_radio = mesh_pb2.FromRadio()
                from_radio.packet.CopyFrom(reply_packet)
                self._send_from_radio(from_radio)
                info("ADMIN", f"[{self._log_prefix()}] Sent get_device_metadata_response (request_id={packet.id})")
            
            elif admin_msg.HasField('set_time_only'):
                timestamp = admin_msg.set_time_only
                result = self.rtc.perhaps_set_rtc(RTCQuality.NTP, timestamp, force_update=False)
                dt = datetime.fromtimestamp(timestamp)
                if result == RTCSetResult.SUCCESS:
                    info("ADMIN", f"[{self._log_prefix()}] Time synchronization: {dt.strftime('%Y-%m-%d %H:%M:%S')} (timestamp: {timestamp})")
                elif result == RTCSetResult.INVALID_TIME:
                    warn("ADMIN", f"[{self._log_prefix()}] Invalid time: {dt.strftime('%Y-%m-%d %H:%M:%S')} (timestamp: {timestamp})")
                else:
                    debug("ADMIN", f"[{self._log_prefix()}] Time not set (quality insufficient): {result.name}")
                
        except Exception as e:
            error("ADMIN", f"[{self._log_prefix()}] Error processing AdminMessage: {e}")
            import traceback
            traceback.print_exc()
    
    def _send_config(self, config_nonce: int) -> None:
        """Отправляет конфигурацию клиенту"""
        info("CONFIG", f"[{self._log_prefix()}] Sending configuration (nonce: {config_nonce})")
        
        # Устанавливаем device_id и проверяем маппинг ДО начала отправки конфигурации
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
            debug("CONFIG", f"[{self._log_prefix()}] Device ID set: {self._device_id.hex()}")
            
            # Обновляем маппинг device_id -> node_id для сохранения настроек между переподключениями
            # ВАЖНО: Делаем это ДО начала отправки конфигурации, чтобы не прерывать процесс
            if self.server:
                device_id_hex = self._device_id.hex()
                with self.server.device_id_lock:
                    # Если device_id уже есть в маппинге, используем сохраненный node_id
                    if device_id_hex in self.server.device_id_to_node_id:
                        saved_node_id = self.server.device_id_to_node_id[device_id_hex]
                        if saved_node_id != self.node_id:
                            info("CONFIG", f"[{self._log_prefix()}] Updating node_id: {self.node_id} -> {saved_node_id} (found saved mapping for device_id)")
                            self.node_id = saved_node_id
                            # Обновляем node_num
                            try:
                                self.node_num = int(self.node_id[1:], 16) if self.node_id.startswith('!') else int(self.node_id, 16)
                                self.node_num = self.node_num & 0x7FFFFFFF
                            except:
                                pass
                            # Обновляем маппинг IP -> node_id для сохранения между перезапусками
                            if self.server and self.client_address:
                                client_ip = self.client_address[0]
                                self.server.ip_to_node_id[client_ip] = self.node_id
                                self.server._save_ip_mapping()
                            # Обновляем Persistence для использования правильного файла
                            self.persistence = Persistence(node_id=self.node_id)
                            # Перезагружаем настройки с правильным node_id (но НЕ создаем новый MQTT клиент)
                            # Просто обновляем настройки, которые уже загружены
                            self._load_settings()
                    else:
                        # Сохраняем новый маппинг device_id -> node_id
                        self.server.device_id_to_node_id[device_id_hex] = self.node_id
                        self.server._save_device_id_mapping()
                        # Также обновляем маппинг IP -> node_id для сохранения между перезапусками
                        if self.client_address:
                            client_ip = self.client_address[0]
                            self.server.ip_to_node_id[client_ip] = self.node_id
                            self.server._save_ip_mapping()
                        info("CONFIG", f"[{self._log_prefix()}] Saved device_id -> node_id mapping: {device_id_hex[:16]}... -> {self.node_id}")
        
        # Используем ConfigSender для отправки конфигурации
        config_sender = ConfigSender(
            node_id=self.node_id,
            node_num=self.node_num,
            owner=self.owner,
            channels=self.channels,
            config_storage=self.config_storage,
            node_db=self.node_db,
            pki_public_key=self.pki_public_key,
            rtc=self.rtc,
            send_from_radio_callback=self._send_from_radio,
            device_id=self._device_id
        )
        config_sender.send_config(config_nonce)
        
        info("CONFIG", f"[{self._log_prefix()}] Configuration sent completely (nonce: {config_nonce})")
    
    def close(self) -> None:
        """Закрывает сессию и очищает ресурсы"""
        # Защита от повторного закрытия
        if self._closed:
            return
        
        self._closed = True
        
        # Останавливаем MQTT клиент ПЕРЕД закрытием сокета
        if self.mqtt_client:
            try:
                self.mqtt_client.stop()
            except Exception as e:
                warn("SESSION", f"[{self._log_prefix()}] Error stopping MQTT client: {e}")
            finally:
                self.mqtt_client = None
        
        # Закрываем TCP сокет
        if self.client_socket:
            try:
                self.client_socket.close()
            except:
                pass
        
        # Даем время на завершение всех потоков
        time.sleep(0.1)
        
        info("SESSION", f"[{self._log_prefix()}] Session closed")

