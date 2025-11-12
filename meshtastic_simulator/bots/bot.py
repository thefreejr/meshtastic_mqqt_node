"""
Класс Bot - узел Meshtastic без TCP интерфейса
"""

import time
import random
import threading
import requests
from typing import Optional, Dict, Any, List
from datetime import datetime

try:
    from meshtastic import mesh_pb2
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
from ..mesh.config_storage import ConfigStorage, NodeConfig
from ..mesh.persistence import Persistence
from ..mesh.rtc import RTC
from ..mesh.settings_loader import SettingsLoader
from ..mesh.utils import generate_node_id
from ..mqtt.client import MQTTClient
from ..mqtt.packet_processor import MQTTPacketProcessor
from ..config import DEFAULT_HOP_LIMIT
from ..utils.logger import info, debug, warn, error
from .message_store import MessageStore
from .bot_config import BotConfig


class Bot:
    """Бот Meshtastic - узел без TCP интерфейса"""
    
    def __init__(self, bot_id: str, config: Dict[str, Any]):
        """
        Инициализирует бота
        
        Args:
            bot_id: Уникальный ID бота
            config: Конфигурация бота (merged с дефолтами)
        """
        self.bot_id = bot_id
        self.config = config
        self.started_at = time.time()
        
        # Генерируем node_id если не указан
        node_id_config = config.get('node_id')
        if node_id_config:
            self.node_id = node_id_config
        else:
            # Генерируем на основе bot_id для стабильности
            import hashlib
            hash_obj = hashlib.md5(bot_id.encode())
            node_num = int.from_bytes(hash_obj.digest()[:4], byteorder='big') & 0x7FFFFFFF
            self.node_id = f"!{node_num:08X}"
        
        # Определяем node_num из node_id
        try:
            self.node_num = int(self.node_id[1:], 16) if self.node_id.startswith('!') else int(self.node_id, 16)
            self.node_num = self.node_num & 0x7FFFFFFF
        except:
            self.node_num = NodeConfig.FALLBACK_NODE_NUM
        
        # Компоненты (как в TCPConnectionSession)
        self.channels = Channels()
        self.node_db = NodeDB(our_node_num=self.node_num)
        self.config_storage = ConfigStorage()
        self.rtc = RTC()
        self.persistence = Persistence(node_id=self.node_id)
        
        # Загружаем каналы из node_defaults.json
        self._load_channels()
        
        # MQTT клиент
        self.mqtt_client: Optional[MQTTClient] = None
        
        # Хранилище сообщений
        self.message_store = MessageStore(bot_id=bot_id)
        
        # Webhook настройки
        webhook_config = config.get('webhook', {})
        self.webhook_url = webhook_config.get('url')
        self.webhook_enabled = webhook_config.get('enabled', False) and self.webhook_url is not None
        self.webhook_timeout = webhook_config.get('timeout', 5)
        self.webhook_retry_count = webhook_config.get('retry_count', 3)
        self.webhook_retry_delay = webhook_config.get('retry_delay', 1)
        self.webhook_headers = webhook_config.get('headers', {})
        self.webhook_verify_ssl = webhook_config.get('verify_ssl', True)
        
        # Владелец (owner)
        self.owner = mesh_pb2.User()
        self.owner.id = self.node_id
        user_config = config.get('user', {})
        self.owner.long_name = user_config.get('long_name', 'Bot')
        self.owner.short_name = user_config.get('short_name', 'BOT')
        hw_model_str = user_config.get('hw_model', 'PORTDUINO')
        self.owner.hw_model = self._hw_model_to_int(hw_model_str)
        self.owner.is_licensed = False
        
        # Загружаем сохраненные настройки
        settings_loader = SettingsLoader(
            persistence=self.persistence,
            channels=self.channels,
            config_storage=self.config_storage,
            owner=self.owner,
            pki_public_key=None,
            node_id=self.node_id
        )
        settings_loader.load_all()
        
        # PKI ключи (если есть в сохраненных настройках)
        self.pki_private_key = None
        self.pki_public_key = None
        if self.owner.public_key and len(self.owner.public_key) == 32:
            self.pki_public_key = self.owner.public_key
        
        # Статистика
        self.stats = {
            'messages_received': 0,
            'messages_sent': 0,
            'webhook_calls': 0,
            'webhook_errors': 0,
            'started_at': self.started_at,
            'last_message_at': None
        }
        
        # Блокировка для потокобезопасности
        self._lock = threading.Lock()
        
        info("BOT", f"Bot initialized: {self.bot_id}, node_id={self.node_id}, node_num={self.node_num:08X}")
    
    def _load_channels(self) -> None:
        """Загружает каналы из node_defaults.json"""
        try:
            from pathlib import Path
            import json
            
            node_defaults_file = Path(__file__).parent.parent.parent / "config" / "node_defaults.json"
            if node_defaults_file.exists():
                with open(node_defaults_file, 'r', encoding='utf-8') as f:
                    node_defaults = json.load(f)
                    channels_config = node_defaults.get('channels', [])
                    
                    # Загружаем каналы
                    for ch_config in channels_config:
                        index = ch_config.get('index', 0)
                        if index < len(self.channels.channels):
                            ch = self.channels.channels[index]
                            if ch_config.get('name'):
                                ch.settings.name = ch_config['name']
                            if ch_config.get('psk'):
                                import base64
                                try:
                                    ch.settings.psk = base64.b64decode(ch_config['psk'])
                                except:
                                    pass
                            ch.settings.uplink_enabled = ch_config.get('uplink_enabled', False)
                            ch.settings.downlink_enabled = ch_config.get('downlink_enabled', False)
        except Exception as e:
            warn("BOT", f"[{self.bot_id}] Error loading channels: {e}")
    
    def _hw_model_to_int(self, hw_model_str: str) -> int:
        """Преобразует строку hw_model в int"""
        hw_model_map = {
            'UNSET': 0,
            'TLORA_V2': 1,
            'TLORA_V1': 2,
            'TLORA_V2_1_1P6': 3,
            'TBEAM': 4,
            'HELTEC_V2_0': 5,
            'TBEAM_V0P7': 6,
            'T_ECHO': 7,
            'TLORA_V1_1P3': 8,
            'RAK4631': 9,
            'HELTEC_V2_1': 10,
            'HELTEC_V1': 11,
            'LILYGO_TBEAM_S3_CORE': 12,
            'RAK11200': 13,
            'NANO_G1': 14,
            'TLORA_V2_1_1P8': 15,
            'TLORA_V3': 16,
            'TLORA_V4': 17,
            'TLORA_V5': 18,
            'TLORA_V6': 19,
            'T_DECK': 20,
            'T_WATCH_S3': 21,
            'PICO': 22,
            'STATION_G1': 23,
            'NANO_G1_EXPLORER': 24,
            'NANO_G2_ULTRA': 25,
            'M5STACK': 26,
            'HELTEC_V3': 27,
            'HELTEC_WSL_V3': 28,
            'BETAFPV_2400_TX': 29,
            'BETAFPV_900_NANO_TX': 30,
            'DIY_V1': 31,
            'NANO_G1_5': 32,
            'STATION_G2': 33,
            'RAK11310': 34,
            'PORTDUINO': 255
        }
        return hw_model_map.get(hw_model_str.upper(), 255)  # PORTDUINO по умолчанию
    
    def start(self) -> bool:
        """Запускает бота (подключается к MQTT)"""
        with self._lock:
            if self.mqtt_client is not None:
                warn("BOT", f"[{self.bot_id}] Bot already started")
                return False
            
            # Получаем MQTT конфигурацию
            bot_config_loader = BotConfig()
            mqtt_config = bot_config_loader.get_mqtt_config(self.config)
            
            # Создаем MQTT клиент
            self.mqtt_client = MQTTClient(
                broker=mqtt_config['address'],
                port=mqtt_config['port'],
                username=mqtt_config['username'],
                password=mqtt_config['password'],
                root_topic=mqtt_config['root'],
                node_id=self.node_id,
                channels=self.channels,
                node_db=self.node_db,
                server=None  # Боты не используют TCP сервер
            )
            
            # Модифицируем packet_processor для сохранения сообщений
            original_process = self.mqtt_client.packet_processor.process_mqtt_message
            
            def process_with_store(msg, to_client_queue):
                result = original_process(msg, to_client_queue)
                # Сохраняем текстовые сообщения
                if result:
                    try:
                        from meshtastic import mqtt_pb2
                        # msg.payload уже является bytes, парсим ServiceEnvelope
                        envelope = mqtt_pb2.ServiceEnvelope()
                        envelope.ParseFromString(msg.payload)
                        if envelope.packet:
                            packet = mesh_pb2.MeshPacket()
                            packet.CopyFrom(envelope.packet)
                            # Проверяем, что это текстовое сообщение
                            if (packet.WhichOneof('payload_variant') == 'decoded' and
                                hasattr(packet.decoded, 'portnum') and
                                packet.decoded.portnum == portnums_pb2.PortNum.TEXT_MESSAGE_APP):
                                with self._lock:
                                    self.message_store.add_message(packet)
                                    self.stats['messages_received'] += 1
                                    self.stats['last_message_at'] = time.time()
                                # Вызываем webhook (вне блокировки)
                                self._call_webhook(packet)
                    except Exception as e:
                        debug("BOT", f"[{self.bot_id}] Error processing message: {e}")
                return result
            
            self.mqtt_client.packet_processor.process_mqtt_message = process_with_store
            
            # Подключаемся к MQTT
            if not self.mqtt_client.start():
                error("BOT", f"[{self.bot_id}] Failed to connect to MQTT")
                self.mqtt_client = None
                return False
            
            info("BOT", f"[{self.bot_id}] Bot started, connected to MQTT: {mqtt_config['address']}:{mqtt_config['port']}")
            
            # Отправляем NodeInfo/Telemetry/Position при подключении
            if self.config.get('auto_send_nodeinfo', True):
                self._publish_node_info_to_mqtt()
            
            if self.config.get('auto_send_telemetry', True):
                self._publish_telemetry_to_mqtt()
            
            if self.config.get('auto_send_position', True):
                self._send_our_position()
            
            return True
    
    def stop(self) -> None:
        """Останавливает бота (отключается от MQTT)"""
        with self._lock:
            if self.mqtt_client:
                self.mqtt_client.stop()
                self.mqtt_client = None
                info("BOT", f"[{self.bot_id}] Bot stopped")
    
    def send_message(self, text: str, destination: Optional[str] = None, 
                     channel: int = 0, want_ack: bool = False) -> Optional[int]:
        """
        Отправляет текстовое сообщение
        
        Args:
            text: Текст сообщения
            destination: Node ID получателя (None = broadcast)
            channel: Индекс канала
            want_ack: Запрос подтверждения доставки
            
        Returns:
            ID пакета или None при ошибке
        """
        if not self.mqtt_client or not self.mqtt_client.connected:
            error("BOT", f"[{self.bot_id}] Cannot send message: MQTT not connected")
            return None
        
        try:
            # Определяем получателя
            if destination:
                try:
                    dest_node_num = int(destination[1:], 16) if destination.startswith('!') else int(destination, 16)
                    dest_node_num = dest_node_num & 0x7FFFFFFF
                except:
                    error("BOT", f"[{self.bot_id}] Invalid destination: {destination}")
                    return None
            else:
                dest_node_num = 0xFFFFFFFF  # Broadcast
            
            # Создаем MeshPacket
            packet = mesh_pb2.MeshPacket()
            packet.id = random.randint(1, 0xFFFFFFFF)
            packet.to = dest_node_num
            setattr(packet, 'from', self.node_num)
            packet.channel = channel if channel < 8 else 0
            packet.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
            packet.decoded.payload = text.encode('utf-8')
            packet.hop_limit = DEFAULT_HOP_LIMIT
            packet.hop_start = DEFAULT_HOP_LIMIT
            packet.want_ack = want_ack
            
            # Отправляем через MQTT
            if self.mqtt_client.publish_packet(packet, channel):
                self.stats['messages_sent'] += 1
                info("BOT", f"[{self.bot_id}] Sent message: to={destination or 'broadcast'}, text={text[:50]}")
                return packet.id
            else:
                error("BOT", f"[{self.bot_id}] Failed to send message")
                return None
        except Exception as e:
            error("BOT", f"[{self.bot_id}] Error sending message: {e}")
            return None
    
    def get_messages(self, limit: int = 50, offset: int = 0, 
                     since: Optional[datetime] = None) -> Dict[str, Any]:
        """Получает список сообщений бота"""
        return self.message_store.get_messages(limit, offset, since)
    
    def _call_webhook(self, packet: mesh_pb2.MeshPacket) -> bool:
        """Вызывает webhook с сообщением"""
        if not self.webhook_enabled:
            return False
        
        try:
            # Извлекаем данные сообщения
            message_text = packet.decoded.payload.decode('utf-8', errors='ignore')
            packet_from = getattr(packet, 'from', 0)
            packet_to = packet.to
            received_at = time.time()
            if hasattr(packet, 'rx_time') and packet.rx_time > 0:
                received_at = packet.rx_time
            
            message_data = {
                'bot_id': self.bot_id,
                'node_id': self.node_id,
                'message': {
                    'id': packet.id,
                    'from': f"!{packet_from:08X}" if packet_from else None,
                    'to': f"!{packet_to:08X}" if packet_to != 0xFFFFFFFF else None,
                    'message': message_text,
                    'received_at': datetime.fromtimestamp(received_at).isoformat(),
                    'channel': packet.channel if packet.channel < 8 else 0,
                    'rssi': getattr(packet, 'rssi', None),
                    'snr': getattr(packet, 'snr', None),
                    'hop_limit': getattr(packet, 'hop_limit', None),
                    'hop_start': getattr(packet, 'hop_start', None)
                }
            }
            
            # Retry логика
            for attempt in range(self.webhook_retry_count):
                try:
                    response = requests.post(
                        self.webhook_url,
                        json=message_data,
                        headers=self.webhook_headers,
                        timeout=self.webhook_timeout,
                        verify=self.webhook_verify_ssl
                    )
                    
                    if response.status_code >= 200 and response.status_code < 300:
                        self.stats['webhook_calls'] += 1
                        debug("BOT", f"[{self.bot_id}] Webhook called successfully: {response.status_code}")
                        return True
                    else:
                        self.stats['webhook_errors'] += 1
                        warn("BOT", f"[{self.bot_id}] Webhook returned error: {response.status_code}")
                        if attempt < self.webhook_retry_count - 1:
                            time.sleep(self.webhook_retry_delay)
                except Exception as e:
                    self.stats['webhook_errors'] += 1
                    warn("BOT", f"[{self.bot_id}] Webhook error (attempt {attempt + 1}/{self.webhook_retry_count}): {e}")
                    if attempt < self.webhook_retry_count - 1:
                        time.sleep(self.webhook_retry_delay)
            
            return False
        except Exception as e:
            error("BOT", f"[{self.bot_id}] Error calling webhook: {e}")
            return False
    
    def _publish_node_info_to_mqtt(self) -> None:
        """Отправляет NodeInfo в MQTT"""
        if not self.mqtt_client or not self.mqtt_client.connected:
            return
        
        try:
            # Обновляем телеметрию
            try:
                if telemetry_pb2:
                    our_node = self.node_db.get_or_create_mesh_node(self.node_num)
                    if not hasattr(our_node, 'device_metrics') or not our_node.HasField('device_metrics'):
                        device_metrics = telemetry_pb2.DeviceMetrics()
                        device_config = self.config.get('device_metrics', {})
                        device_metrics.battery_level = device_config.get('battery_level', 101)
                        device_metrics.voltage = device_config.get('voltage', 5.0)
                        device_metrics.channel_utilization = device_config.get('channel_utilization', 0.0)
                        device_metrics.air_util_tx = device_config.get('air_util_tx', 0.0)
                        device_metrics.uptime_seconds = self.get_uptime_seconds()
                        our_node.device_metrics.CopyFrom(device_metrics)
                    else:
                        our_node.device_metrics.uptime_seconds = self.get_uptime_seconds()
            except Exception as e:
                debug("BOT", f"[{self.bot_id}] Error updating telemetry: {e}")
            
            # Создаем User пакет
            user = mesh_pb2.User()
            user.id = self.owner.id
            user.long_name = self.owner.long_name
            user.short_name = self.owner.short_name
            user.is_licensed = self.owner.is_licensed
            if self.owner.public_key and len(self.owner.public_key) > 0:
                if not self.owner.is_licensed:
                    user.public_key = self.owner.public_key
            
            # Создаем MeshPacket
            packet = mesh_pb2.MeshPacket()
            packet.id = random.randint(1, 0xFFFFFFFF)
            packet.to = 0xFFFFFFFF  # Broadcast
            setattr(packet, 'from', self.node_num)
            packet.channel = 0
            packet.decoded.portnum = portnums_pb2.PortNum.NODEINFO_APP
            packet.decoded.payload = user.SerializeToString()
            packet.hop_limit = DEFAULT_HOP_LIMIT
            packet.hop_start = DEFAULT_HOP_LIMIT
            packet.want_ack = False
            
            # Отправляем
            self.mqtt_client.publish_packet(packet, 0)
            info("BOT", f"[{self.bot_id}] Published NodeInfo to MQTT")
        except Exception as e:
            error("BOT", f"[{self.bot_id}] Error publishing NodeInfo: {e}")
    
    def _publish_telemetry_to_mqtt(self) -> None:
        """Отправляет телеметрию в MQTT"""
        if not self.mqtt_client or not self.mqtt_client.connected:
            return
        
        try:
            if not telemetry_pb2:
                return
            
            our_node = self.node_db.get_or_create_mesh_node(self.node_num)
            if not hasattr(our_node, 'device_metrics') or not our_node.HasField('device_metrics'):
                device_metrics = telemetry_pb2.DeviceMetrics()
                device_config = self.config.get('device_metrics', {})
                device_metrics.battery_level = device_config.get('battery_level', 101)
                device_metrics.voltage = device_config.get('voltage', 5.0)
                device_metrics.channel_utilization = device_config.get('channel_utilization', 0.0)
                device_metrics.air_util_tx = device_config.get('air_util_tx', 0.0)
                device_metrics.uptime_seconds = self.get_uptime_seconds()
                our_node.device_metrics.CopyFrom(device_metrics)
            else:
                our_node.device_metrics.uptime_seconds = self.get_uptime_seconds()
            
            # Создаем Telemetry пакет
            telemetry = telemetry_pb2.Telemetry()
            telemetry.time = int(time.time())
            telemetry.device_metrics.CopyFrom(our_node.device_metrics)
            
            # Создаем MeshPacket
            packet = mesh_pb2.MeshPacket()
            packet.id = random.randint(1, 0xFFFFFFFF)
            packet.to = 0xFFFFFFFF  # Broadcast
            setattr(packet, 'from', self.node_num)
            packet.channel = 0
            packet.decoded.portnum = portnums_pb2.PortNum.TELEMETRY_APP
            packet.decoded.payload = telemetry.SerializeToString()
            packet.hop_limit = DEFAULT_HOP_LIMIT
            packet.hop_start = DEFAULT_HOP_LIMIT
            packet.want_ack = False
            
            # Отправляем
            self.mqtt_client.publish_packet(packet, 0)
            info("BOT", f"[{self.bot_id}] Published Telemetry to MQTT")
        except Exception as e:
            error("BOT", f"[{self.bot_id}] Error publishing Telemetry: {e}")
    
    def _send_our_position(self) -> None:
        """Отправляет позицию в MQTT"""
        if not self.mqtt_client or not self.mqtt_client.connected:
            return
        
        try:
            our_node = self.node_db.get_or_create_mesh_node(self.node_num)
            if not hasattr(our_node, 'position') or not our_node.HasField('position'):
                # Нет позиции - пропускаем
                return
            
            # Создаем MeshPacket с позицией
            packet = mesh_pb2.MeshPacket()
            packet.id = random.randint(1, 0xFFFFFFFF)
            packet.to = 0xFFFFFFFF  # Broadcast
            setattr(packet, 'from', self.node_num)
            packet.channel = 0
            packet.decoded.portnum = portnums_pb2.PortNum.POSITION_APP
            packet.decoded.payload = our_node.position.SerializeToString()
            packet.hop_limit = DEFAULT_HOP_LIMIT
            packet.hop_start = DEFAULT_HOP_LIMIT
            packet.want_ack = False
            
            # Отправляем
            self.mqtt_client.publish_packet(packet, 0)
            info("BOT", f"[{self.bot_id}] Published Position to MQTT")
        except Exception as e:
            debug("BOT", f"[{self.bot_id}] Error publishing Position: {e}")
    
    def get_uptime_seconds(self) -> int:
        """Возвращает uptime в секундах"""
        return int(time.time() - self.started_at)
    
    def get_statistics(self) -> Dict[str, Any]:
        """Возвращает статистику бота"""
        with self._lock:
            return {
                'bot_id': self.bot_id,
                'node_id': self.node_id,
                'node_num': self.node_num,
                'mqtt_connected': self.mqtt_client.connected if self.mqtt_client else False,
                'messages_received': self.stats['messages_received'],
                'messages_sent': self.stats['messages_sent'],
                'webhook_calls': self.stats['webhook_calls'],
                'webhook_errors': self.stats['webhook_errors'],
                'started_at': datetime.fromtimestamp(self.stats['started_at']).isoformat(),
                'last_message_at': datetime.fromtimestamp(self.stats['last_message_at']).isoformat() if self.stats['last_message_at'] else None,
                'uptime_seconds': self.get_uptime_seconds()
            }

