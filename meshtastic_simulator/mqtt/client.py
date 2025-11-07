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
                 root_topic: str, node_id: str, channels: Channels, node_db: NodeDB = None):
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.root_topic = root_topic
        self.node_id = node_id
        self.channels = channels
        self.node_db = node_db
        self.to_client_queue = queue.Queue()
        
        # Инициализируем модули
        self.connection = None
        self.subscription = MQTTSubscription(root_topic, channels, node_id)
        self.packet_processor = MQTTPacketProcessor(node_id, channels, node_db)
    
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
                    info("MQTT", f"Обновлен адрес сервера: {old_broker}:{old_port} -> {self.broker}:{self.port}")
                else:
                    # Адрес пустой - используем дефолтный (как в firmware)
                    self.broker = DEFAULT_MQTT_ADDRESS
                    self.port = 8883 if mqtt_config.tls_enabled else 1883
                    info("MQTT", f"Адрес пустой, используем дефолтный: {old_broker}:{old_port} -> {self.broker}:{self.port}")
            else:
                # Адрес не установлен - используем дефолтный
                self.broker = DEFAULT_MQTT_ADDRESS
                self.port = 8883 if mqtt_config.tls_enabled else 1883
                info("MQTT", f"Адрес не установлен, используем дефолтный: {old_broker}:{old_port} -> {self.broker}:{self.port}")
            
            # Обновляем логин (как в firmware PubSubConfig)
            if hasattr(mqtt_config, 'username') and mqtt_config.username:
                new_username = mqtt_config.username.strip()
                if new_username:
                    self.username = new_username
                    info("MQTT", f"Обновлен логин MQTT: {old_username} -> {self.username}")
                else:
                    # Логин пустой - используем дефолтный (как в firmware)
                    self.username = DEFAULT_MQTT_USERNAME
                    info("MQTT", f"Логин пустой, используем дефолтный: {old_username} -> {self.username}")
            else:
                # Логин не установлен - используем дефолтный
                self.username = DEFAULT_MQTT_USERNAME
                debug("MQTT", f"Логин не установлен, используем дефолтный: {old_username} -> {self.username}")
            
            # Обновляем пароль (как в firmware PubSubConfig)
            if hasattr(mqtt_config, 'password') and mqtt_config.password:
                new_password = mqtt_config.password.strip()
                if new_password:
                    self.password = new_password
                    info("MQTT", "Обновлен пароль MQTT")
                else:
                    # Пароль пустой - используем дефолтный (как в firmware)
                    self.password = DEFAULT_MQTT_PASSWORD
                    info("MQTT", "Пароль пустой, используем дефолтный")
            else:
                # Пароль не установлен - используем дефолтный
                self.password = DEFAULT_MQTT_PASSWORD
                debug("MQTT", "Пароль не установлен, используем дефолтный")
            
            # Обновляем корневой топик (как в firmware)
            if hasattr(mqtt_config, 'root') and mqtt_config.root:
                new_root = mqtt_config.root.strip()
                if new_root:
                    self.root_topic = new_root
                    info("MQTT", f"Обновлен корневой топик: {old_root} -> {self.root_topic}")
                else:
                    # Корневой топик пустой - используем дефолтный (как в firmware)
                    self.root_topic = DEFAULT_MQTT_ROOT
                    info("MQTT", f"Корневой топик пустой, используем дефолтный: {old_root} -> {self.root_topic}")
            else:
                # Корневой топик не установлен - используем дефолтный
                self.root_topic = DEFAULT_MQTT_ROOT
                debug("MQTT", f"Корневой топик не установлен, используем дефолтный: {old_root} -> {self.root_topic}")
            
            # Проверяем, изменились ли настройки
            settings_changed = (old_broker != self.broker or old_port != self.port or 
                              old_username != self.username or old_password != self.password or 
                              old_root != self.root_topic)
            
            info("MQTT", f"Проверка изменений настроек: changed={settings_changed}, old={old_broker}:{old_port}, new={self.broker}:{self.port}")
            
            # Обновляем модули с новыми настройками
            if settings_changed:
                if self.connection and self.connection.is_connected():
                    info("MQTT", "Переподключение с новыми настройками...")
                    self.stop()
                    time.sleep(1)
                # Если не подключен, просто запускаем с новыми настройками
                return self.start()
            
            # Обновляем subscription с новым root_topic
            self.subscription = MQTTSubscription(self.root_topic, self.channels, self.node_id)
            
            return True
        except Exception as e:
            error("MQTT", f"Ошибка обновления настроек MQTT: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def start(self):
        """Запускает MQTT клиент"""
        # Создаем callback для подписки
        def on_connect_callback(client, userdata, flags, rc, properties=None, reasonCode=None):
            if rc == 0:
                info("MQTT", f"Подключен к {self.broker}:{self.port}")
                # РџРѕРґРїРёСЃС‹РІР°РµРјСЃСЏ РЅР° РєР°РЅР°Р»С‹
                self.subscription.subscribe_to_channels(client)
        
        # Создаем callback для обработки сообщений
        def on_message_callback(client, userdata, msg):
            self.packet_processor.process_mqtt_message(msg, self.to_client_queue)
        
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
        
        return self.connection.connect()
    
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
                    debug("MQTT", f"Отправка пакета: hops_away={hops_away}, hop_start={hop_start}, hop_limit={hop_limit}")
            # Не отправляем пакеты, которые уже пришли из MQTT (как в firmware)
            if hasattr(packet, 'via_mqtt') and packet.via_mqtt:
                debug("MQTT", "Пропуск публикации: пакет уже из MQTT")
                return False
            
            # Не отправляем Admin пакеты в MQTT (как в firmware MQTT::onReceive - игнорируются Admin пакеты)
            if packet.WhichOneof('payload_variant') == 'decoded':
                if hasattr(packet.decoded, 'portnum') and packet.decoded.portnum == portnums_pb2.PortNum.ADMIN_APP:
                    debug("MQTT", "Пропуск публикации: Admin пакеты не отправляются в MQTT")
                    return False
            
            ch = self.channels.get_by_index(channel_index)
            if not ch.settings.uplink_enabled:
                debug("MQTT", f"Пропуск публикации: канал {channel_index} не имеет uplink_enabled")
                return False
            
            if not self.channels.any_mqtt_enabled():
                debug("MQTT", "Пропуск публикации: нет каналов с uplink_enabled")
                return False
            
            channel_id = self.channels.get_global_id(channel_index)
            
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
                    info("MQTT", f"📤 CUSTOM ПАКЕТ ОТПРАВЛЯЕТСЯ: topic={topic}, gateway_id={self.node_id}, channel_id={channel_id}, payload_size={len(payload)}")
                self.client.publish(topic, payload)
                if channel_id == "Custom":
                    info("MQTT", f"✅ CUSTOM ПАКЕТ ОТПРАВЛЕН: topic={topic}")
                else:
                    info("MQTT", f"Отправлен пакет: {topic} (канал {channel_index}: {channel_id})")
                return True
            else:
                warn("MQTT", "MQTT не подключен, пакет не отправлен")
                return False
        except Exception as e:
            error("MQTT", f"Ошибка публикации пакета: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def stop(self):
        """Останавливает MQTT клиент"""
        if self.connection:
            try:
                self.connection.disconnect()
            except Exception as e:
                warn("MQTT", f"Ошибка остановки MQTT соединения: {e}")
            finally:
                self.connection = None

