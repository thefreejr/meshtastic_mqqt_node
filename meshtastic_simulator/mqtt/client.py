"""
MQTT клиент для подключения к брокеру
"""

import queue
import time

from ..utils.logger import debug, info, warn, error

try:
    from meshtastic import mesh_pb2, mqtt_pb2
    from meshtastic.protobuf import portnums_pb2
except ImportError:
    print("Ошибка: Установите meshtastic: pip install meshtastic")
    raise

from ..mesh.channels import Channels
from ..mesh.node_db import NodeDB
from .connection import MQTTConnection
from .subscription import MQTTSubscription
from .packet_processor import MQTTPacketProcessor


class MQTTClient:
    """MQTT клиент для подключения к брокеру"""
    
    def __init__(self, broker: str, port: int, username: str, password: str, 
                 root_topic: str, node_id: str, channels: Channels, node_db: NodeDB = None,
                 server = None):
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.root_topic = root_topic
        self.node_id = node_id
        self.channels = channels
        self.node_db = node_db
        self.server = server  # Ссылка на TCPServer для доступа к сессиям
        self.to_client_queue = queue.Queue()
        
        # Инициализируем модули
        self.connection = None
        self.subscription = MQTTSubscription(root_topic, channels, node_id)
        self.packet_processor = MQTTPacketProcessor(node_id, channels, node_db, server)
        
        # Флаг для предотвращения повторной остановки
        self._stopped = False
        # Флаг ошибки авторизации (сохраняется между пересозданиями connection)
        self._auth_failed = False
        # Флаг для предотвращения повторных попыток подключения
        self._connecting = False
    
    def update_config(self, mqtt_config):
        """
        Обновляет настройки MQTT из module_config.mqtt (как в firmware MQTT::reconnect)
        Если адрес/логин/пароль пустые - использует дефолтные значения из config.py
        """
        try:
            from meshtastic.protobuf import module_config_pb2
            from ..config import DEFAULT_MQTT_ADDRESS, DEFAULT_MQTT_USERNAME, DEFAULT_MQTT_PASSWORD, DEFAULT_MQTT_ROOT
            
            # ВАЖНО: Сохраняем старые настройки ДО обновления для корректного сравнения
            old_broker = self.broker
            old_port = self.port
            old_username = self.username
            old_password = self.password
            old_root = self.root_topic
            
            # Обновляем адрес сервера (как в firmware PubSubConfig)
            if hasattr(mqtt_config, 'address') and mqtt_config.address:
                new_broker = mqtt_config.address.strip()
                if new_broker:
                    # Парсим адрес и порт (как в firmware parseHostAndPort)
                    if ':' in new_broker:
                        parts = new_broker.split(':')
                        self.broker = parts[0]
                        try:
                            self.port = int(parts[1])
                        except:
                            self.port = 8883 if mqtt_config.tls_enabled else 1883
                    else:
                        self.broker = new_broker
                        # Порт определяется из tls_enabled
                        self.port = 8883 if mqtt_config.tls_enabled else 1883
                    info("MQTT", f"Updated server address: {old_broker}:{old_port} -> {self.broker}:{self.port}")
                else:
                    # Адрес пустой - используем дефолтный (как в firmware)
                    self.broker = DEFAULT_MQTT_ADDRESS
                    self.port = 8883 if mqtt_config.tls_enabled else 1883
                    info("MQTT", f"Address empty, using default: {old_broker}:{old_port} -> {self.broker}:{self.port}")
            else:
                # Адрес не установлен - используем дефолтный
                self.broker = DEFAULT_MQTT_ADDRESS
                self.port = 8883 if mqtt_config.tls_enabled else 1883
                info("MQTT", f"Address not set, using default: {old_broker}:{old_port} -> {self.broker}:{self.port}")
            
            # Обновляем логин (как в firmware PubSubConfig)
            if hasattr(mqtt_config, 'username') and mqtt_config.username:
                new_username = mqtt_config.username.strip()
                if new_username:
                    self.username = new_username
                    info("MQTT", f"Updated MQTT username: {old_username} -> {self.username}")
                else:
                    # Логин пустой - используем дефолтный (как в firmware)
                    self.username = DEFAULT_MQTT_USERNAME
                    info("MQTT", f"Username empty, using default: {old_username} -> {self.username}")
            else:
                # Логин не установлен - используем дефолтный
                self.username = DEFAULT_MQTT_USERNAME
                debug("MQTT", f"Username not set, using default: {old_username} -> {self.username}")
            
            # Обновляем пароль (как в firmware PubSubConfig)
            if hasattr(mqtt_config, 'password') and mqtt_config.password:
                new_password = mqtt_config.password.strip()
                if new_password:
                    self.password = new_password
                    info("MQTT", f"Updated MQTT password (length: {len(new_password)})")
                else:
                    # Пароль пустой - используем дефолтный (как в firmware)
                    self.password = DEFAULT_MQTT_PASSWORD
                    info("MQTT", f"Password empty, using default (length: {len(DEFAULT_MQTT_PASSWORD) if DEFAULT_MQTT_PASSWORD else 0})")
            else:
                # Пароль не установлен - используем дефолтный
                self.password = DEFAULT_MQTT_PASSWORD
                debug("MQTT", f"Password not set, using default (length: {len(DEFAULT_MQTT_PASSWORD) if DEFAULT_MQTT_PASSWORD else 0})")
            
            # ВАЖНО: Сброс флага ошибки авторизации при изменении настроек выполняется ниже,
            # в блоке if settings_changed, чтобы избежать дублирования кода
            
            # Обновляем корневой топик (как в firmware)
            if hasattr(mqtt_config, 'root') and mqtt_config.root:
                new_root = mqtt_config.root.strip()
                if new_root:
                    self.root_topic = new_root
                    info("MQTT", f"Updated root topic: {old_root} -> {self.root_topic}")
                else:
                    # Корневой топик пустой - используем дефолтный (как в firmware)
                    self.root_topic = DEFAULT_MQTT_ROOT
                    info("MQTT", f"Root topic empty, using default: {old_root} -> {self.root_topic}")
            else:
                # Корневой топик не установлен - используем дефолтный
                self.root_topic = DEFAULT_MQTT_ROOT
                debug("MQTT", f"Root topic not set, using default: {old_root} -> {self.root_topic}")
            
            # Проверяем, изменились ли настройки
            settings_changed = (old_broker != self.broker or old_port != self.port or 
                              old_username != self.username or old_password != self.password or 
                              old_root != self.root_topic)
            
            info("MQTT", f"Checking settings changes: changed={settings_changed}, old={old_broker}:{old_port}, new={self.broker}:{self.port}")
            
            # Обновляем модули с новыми настройками
            if settings_changed:
                # Сбрасываем флаг ошибки авторизации при изменении настроек
                # (пользователь мог исправить учетные данные)
                if self._auth_failed:
                    self._auth_failed = False
                    if self.connection and hasattr(self.connection, '_auth_failed'):
                        self.connection._auth_failed = False
                        self.connection._reconnect_stop = False
                        info("MQTT", f"[{self.node_id}] MQTT settings changed, resetting auth failure flag and enabling reconnection")
                
                if self.connection and self.connection.is_connected():
                    info("MQTT", f"[{self.node_id}] Reconnecting with new settings...")
                    self.stop()
                    time.sleep(1)
                # Если не подключен, просто запускаем с новыми настройками
                return self.start()
            
            # Обновляем subscription с новым root_topic
            self.subscription = MQTTSubscription(self.root_topic, self.channels, self.node_id)
            
            return True
        except Exception as e:
            error("MQTT", f"Error updating MQTT settings: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def start(self):
        """Запускает MQTT клиент"""
        # Если уже идет подключение, не запускаем повторно
        if self._connecting:
            debug("MQTT", f"[{self.node_id}] Connection attempt already in progress, skipping")
            return False
        
        # Восстанавливаем флаг ошибки авторизации из предыдущего подключения
        if self._auth_failed:
            warn("MQTT", f"[{self.node_id}] Skipping connection attempt: authentication failed previously. Please update MQTT settings via AdminMessage.")
            return False
        
        # Устанавливаем флаг подключения
        self._connecting = True
        
        try:
            # Создаем callback для подписки
            def on_connect_callback(client, userdata, flags, rc, properties=None, reasonCode=None):
                if rc == 0:
                    # Сообщение о подключении уже выводится в MQTTConnection._on_connect
                    # Сбрасываем флаг ошибки авторизации при успешном подключении
                    self._auth_failed = False
                    self._connecting = False
                    # Подписываемся на каналы
                    self.subscription.subscribe_to_channels(client)
                else:
                    # При ошибке подключения сбрасываем флаг подключения
                    self._connecting = False
            
            # Создаем callback для обработки сообщений
            def on_message_callback(client, userdata, msg):
                self.packet_processor.process_mqtt_message(msg, self.to_client_queue)
            
            # Создаем callback для обновления флага ошибки авторизации
            def on_auth_failed_callback():
                self._auth_failed = True
                self._connecting = False
            
            # Создаем подключение
            self.connection = MQTTConnection(
                broker=self.broker,
                port=self.port,
                username=self.username,
                password=self.password,
                node_id=self.node_id,
                on_connect_callback=on_connect_callback,
                on_message_callback=on_message_callback
            )
            
            # Устанавливаем callback для обновления флага ошибки авторизации
            self.connection._auth_failed_callback = on_auth_failed_callback
            
            result = self.connection.connect()
            
            # Если подключение не удалось, сбрасываем флаг подключения
            if not result:
                self._connecting = False
            
            return result
        except Exception as e:
            error("MQTT", f"[{self.node_id}] Error in start(): {e}")
            self._connecting = False
            return False
    
    @property
    def connected(self):
        """Проверяет, подключен ли клиент"""
        return self.connection.is_connected() if self.connection else False
    
    @property
    def client(self):
        """Возвращает объект paho.mqtt.client"""
        return self.connection.get_client() if self.connection else None
    
    def publish_packet(self, packet: mesh_pb2.MeshPacket, channel_index: int):
        """Публикует пакет в MQTT (как в firmware MQTT::onSend)"""
        try:
            # Логируем информацию о трассировке маршрута для исходящих пакетов
            hop_limit = getattr(packet, 'hop_limit', 0)
            hop_start = getattr(packet, 'hop_start', 0)
            hops_away = 0
            if hop_start != 0 and hop_limit <= hop_start:
                hops_away = hop_start - hop_limit
                if hops_away > 0:
                    debug("MQTT", f"Sending packet: hops_away={hops_away}, hop_start={hop_start}, hop_limit={hop_limit}")
            # Не отправляем пакеты, которые уже пришли из MQTT (как в firmware)
            if hasattr(packet, 'via_mqtt') and packet.via_mqtt:
                debug("MQTT", "Skipping publication: packet already from MQTT")
                return False
            
            # Не отправляем Admin пакеты в MQTT (как в firmware MQTT::onReceive - игнорируются Admin пакеты)
            if packet.WhichOneof('payload_variant') == 'decoded':
                if hasattr(packet.decoded, 'portnum') and packet.decoded.portnum == portnums_pb2.PortNum.ADMIN_APP:
                    debug("MQTT", "Skipping publication: Admin packets are not sent to MQTT")
                    return False
            
            ch = self.channels.get_by_index(channel_index)
            if not ch.settings.uplink_enabled:
                debug("MQTT", f"Skipping publication: channel {channel_index} does not have uplink_enabled")
                return False
            
            if not self.channels.any_mqtt_enabled():
                debug("MQTT", "Skipping publication: no channels with uplink_enabled")
                return False
            
            channel_id = self.channels.get_global_id(channel_index)
            
            # Логируем поле from перед отправкой
            packet_from = getattr(packet, 'from', 0)
            debug("MQTT", f"Publishing packet: from={packet_from:08X}, to={packet.to:08X}, id={packet.id}, channel={channel_index}")
            
            envelope = mqtt_pb2.ServiceEnvelope()
            envelope.packet.CopyFrom(packet)
            envelope.channel_id = channel_id
            envelope.gateway_id = self.node_id
            
            payload = envelope.SerializeToString()
            
            crypt_topic = f"{self.root_topic}/2/e/"
            topic = f"{crypt_topic}{channel_id}/{self.node_id}"
            
            if self.connected and self.client:
                # Для Custom канала добавляем детальное логирование
                if channel_id == "Custom":
                    info("MQTT", f"📤 CUSTOM PACKET SENDING: topic={topic}, gateway_id={self.node_id}, channel_id={channel_id}, from={packet_from:08X}, payload_size={len(payload)}")
                self.client.publish(topic, payload)
                if channel_id == "Custom":
                    info("MQTT", f"✅ CUSTOM PACKET SENT: topic={topic}, from={packet_from:08X}")
                else:
                    info("MQTT", f"Packet sent: {topic} (channel {channel_index}: {channel_id}, from={packet_from:08X})")
                return True
            else:
                warn("MQTT", "MQTT not connected, packet not sent")
                return False
        except Exception as e:
            error("MQTT", f"Error publishing packet: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def stop(self):
        """Останавливает MQTT клиент"""
        # Защита от повторной остановки
        if self._stopped:
            return
        
        self._stopped = True
        
        if self.connection:
            try:
                self.connection.disconnect()
            except Exception as e:
                warn("MQTT", f"Error stopping MQTT connection: {e}")
            finally:
                self.connection = None

