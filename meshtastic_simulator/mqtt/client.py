"""
MQTT клиент для подключения к брокеру
"""

import queue
import ssl
import struct
import time

from ..utils.logger import log, debug, info, warn, error, LogLevel
from ..mesh.rtc import RTCQuality, get_valid_time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Ошибка: Установите paho-mqtt: pip install paho-mqtt")
    raise

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

from ..config import MAX_NUM_CHANNELS
from ..protocol.stream_api import StreamAPI
from ..mesh.channels import Channels
from ..mesh.node_db import NodeDB


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
        self.client = None
        self.connected = False
        self.to_client_queue = queue.Queue()
    
    def update_config(self, mqtt_config):
        """
        Обновляет настройки MQTT из module_config.mqtt (как в firmware MQTT::reconnect)
        Если адрес/логин/пароль пустые - использует дефолтные значения из config.py
        """
        try:
            from meshtastic.protobuf import module_config_pb2
            from ..config import DEFAULT_MQTT_ADDRESS, DEFAULT_MQTT_USERNAME, DEFAULT_MQTT_PASSWORD, DEFAULT_MQTT_ROOT
            
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
                    info("MQTT", f"Обновлен адрес сервера: {self.broker}:{self.port}")
                else:
                    # Адрес пустой - используем дефолтный (как в firmware)
                    self.broker = DEFAULT_MQTT_ADDRESS
                    self.port = 8883 if mqtt_config.tls_enabled else 1883
                    info("MQTT", f"Адрес пустой, используем дефолтный: {self.broker}:{self.port}")
            else:
                # Адрес не установлен - используем дефолтный
                self.broker = DEFAULT_MQTT_ADDRESS
                self.port = 8883 if mqtt_config.tls_enabled else 1883
                info("MQTT", f"Адрес не установлен, используем дефолтный: {self.broker}:{self.port}")
            
            # Обновляем логин (как в firmware PubSubConfig)
            if hasattr(mqtt_config, 'username') and mqtt_config.username:
                new_username = mqtt_config.username.strip()
                if new_username:
                    self.username = new_username
                    info("MQTT", "Обновлен логин MQTT")
                else:
                    # Логин пустой - используем дефолтный (как в firmware)
                    self.username = DEFAULT_MQTT_USERNAME
                    info("MQTT", "Логин пустой, используем дефолтный")
            else:
                # Логин не установлен - используем дефолтный
                self.username = DEFAULT_MQTT_USERNAME
                debug("MQTT", "Логин не установлен, используем дефолтный")
            
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
                    info("MQTT", f"Обновлен корневой топик: {self.root_topic}")
                else:
                    # Корневой топик пустой - используем дефолтный (как в firmware)
                    self.root_topic = DEFAULT_MQTT_ROOT
                    info("MQTT", f"Корневой топик пустой, используем дефолтный: {self.root_topic}")
            else:
                # Корневой топик не установлен - используем дефолтный
                self.root_topic = DEFAULT_MQTT_ROOT
                debug("MQTT", f"Корневой топик не установлен, используем дефолтный: {self.root_topic}")
            
            # Переподключаемся если уже подключены
            if self.connected:
                info("MQTT", "Переподключение с новыми настройками...")
                self.stop()
                time.sleep(1)
                return self.start()
            
            return True
        except Exception as e:
            error("MQTT", f"Ошибка обновления настроек MQTT: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def start(self):
        """Запускает MQTT клиент"""
        try:
            if hasattr(mqtt, 'CallbackAPIVersion'):
                if hasattr(mqtt.CallbackAPIVersion, 'VERSION2'):
                    self.client = mqtt.Client(
                        client_id=self.node_id,
                        callback_api_version=mqtt.CallbackAPIVersion.VERSION2
                    )
                else:
                    self.client = mqtt.Client(client_id=self.node_id)
            else:
                self.client = mqtt.Client(client_id=self.node_id)
        except:
            self.client = mqtt.Client(client_id=self.node_id)
        
        self.client.username_pw_set(self.username, self.password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        
        if self.port == 8883:
            self.client.tls_set(cert_reqs=ssl.CERT_NONE)
            self.client.tls_insecure_set(True)
        
        try:
            self.client.connect(self.broker, self.port, 60)
            self.client.loop_start()
            time.sleep(1)
            return self.connected
        except Exception as e:
            error("MQTT", f"Ошибка подключения: {e}")
            return False
    
    def _on_connect(self, client, userdata, flags, rc, properties=None, reasonCode=None):
        """Callback при подключении к MQTT"""
        if rc == 0:
            self.connected = True
            info("MQTT", f"Подключен к {self.broker}:{self.port}")
            self._send_subscriptions(client)
        else:
            error("MQTT", f"Ошибка подключения (код: {rc})")
    
    def _send_subscriptions(self, client):
        """Подписывается на MQTT топики каналов"""
        has_downlink = False
        crypt_topic = f"{self.root_topic}/2/e/"
        
        for i in range(MAX_NUM_CHANNELS):
            ch = self.channels.get_by_index(i)
            if ch.settings.downlink_enabled:
                has_downlink = True
                channel_id = self.channels.get_global_id(i)
                topic = f"{crypt_topic}{channel_id}/+"
                result, mid = client.subscribe(topic, qos=1)
                if result == 0:
                    info("MQTT", f"Подписан на топик: {topic} (канал {i}: {channel_id})")
                else:
                    error("MQTT", f"Ошибка подписки на топик: {topic} (код: {result})")
        
        if has_downlink:
            topic = f"{crypt_topic}PKI/+"
            result, mid = client.subscribe(topic, qos=1)
            if result == 0:
                info("MQTT", f"Подписан на топик: {topic}")
            else:
                error("MQTT", f"Ошибка подписки на топик: {topic} (код: {result})")
    
    def _on_disconnect(self, client, userdata, rc, properties=None, reasonCode=None):
        """Callback при отключении от MQTT"""
        self.connected = False
        info("MQTT", "Отключен от брокера")
    
    def _on_message(self, client, userdata, msg):
        """Обработка входящих MQTT сообщений"""
        # Импортируем весь код обработки из оригинального файла
        # Для упрощения создаем здесь stub, который будет расширен
        try:
            envelope = mqtt_pb2.ServiceEnvelope()
            envelope.ParseFromString(msg.payload)
            
            if not envelope.packet or not envelope.channel_id:
                warn("MQTT", "Неверный ServiceEnvelope: отсутствует packet или channel_id")
                return
            
            ch = self.channels.get_by_name(envelope.channel_id)
            channel_global_id = self.channels.get_global_id(ch.index)
            
            channel_allowed = False
            if envelope.channel_id == "PKI":
                channel_allowed = True
            else:
                if envelope.channel_id == channel_global_id and ch.settings.downlink_enabled:
                    channel_allowed = True
            
            if not channel_allowed:
                debug("MQTT", f"Пропуск пакета: канал '{envelope.channel_id}' не разрешен")
                return
            
            if envelope.gateway_id == self.node_id:
                debug("MQTT", "Игнорируем свой собственный пакет")
                return
            
            info("MQTT", f"Получен пакет от {envelope.gateway_id} на канале {envelope.channel_id}")
            
            packet = mesh_pb2.MeshPacket()
            packet.CopyFrom(envelope.packet)
            
            setattr(packet, 'from', getattr(envelope.packet, 'from', 0))
            packet.to = getattr(envelope.packet, 'to', 0)
            packet.id = getattr(envelope.packet, 'id', 0)
            packet.channel = getattr(envelope.packet, 'channel', 0)
            packet.hop_limit = getattr(envelope.packet, 'hop_limit', 0)
            packet.hop_start = getattr(envelope.packet, 'hop_start', 0)
            packet.want_ack = getattr(envelope.packet, 'want_ack', False)
            
            # Логируем информацию о трассировке маршрута для пакетов из MQTT
            hops_away = 0
            if packet.hop_start != 0 and packet.hop_limit <= packet.hop_start:
                hops_away = packet.hop_start - packet.hop_limit
                if hops_away > 0:
                    debug("MQTT", f"Трассировка маршрута: hops_away={hops_away}, hop_start={packet.hop_start}, hop_limit={packet.hop_limit}")
            
            payload_type = packet.WhichOneof('payload_variant')
            
            if payload_type == 'encrypted':
                encrypted_data = packet.encrypted if hasattr(packet, 'encrypted') else b''
                
                if encrypted_data:
                    encrypted_size = len(encrypted_data)
                    
                    decrypted = False
                    packet_from = getattr(packet, 'from', 0)
                    packet_to = packet.to
                    MESHTASTIC_PKC_OVERHEAD = 12
                    
                    is_broadcast = packet_to == 0xFFFFFFFF or packet_to == 0xFFFFFFFE
                    is_to_us = packet_to == self.node_db.our_node_num if self.node_db else False
                    
                    if (packet.channel == 0 and is_to_us and packet_to > 0 and not is_broadcast and
                        encrypted_size > MESHTASTIC_PKC_OVERHEAD and self.node_db):
                        from_node = self.node_db.get_mesh_node(packet_from)
                        to_node = self.node_db.get_mesh_node(packet_to)
                        
                        if (from_node and to_node and 
                            hasattr(from_node.user, 'public_key') and len(from_node.user.public_key) == 32 and
                            hasattr(to_node.user, 'public_key') and len(to_node.user.public_key) == 32):
                            debug("PKI", f"Попытка PKI расшифровки (от !{packet_from:08X} к !{packet_to:08X})")
                            warn("PKI", "PKI расшифровка пока не реализована (требуется Curve25519)")
                    
                    if not decrypted:
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
                                            decrypted = True
                                            info("MQTT", f"Пакет расшифрован с канала {ch_idx} (hash={channel_hash})")
                                            ch = self.channels.get_by_index(ch_idx)
                                            break
                                    except Exception as e:
                                        continue
                                except Exception as e:
                                    continue
                        
                        if not decrypted:
                            warn("MQTT", f"Не удалось расшифровать пакет (hash={channel_hash})")
            
            if hasattr(packet, 'via_mqtt'):
                packet.via_mqtt = True
            if hasattr(packet, 'transport_mechanism'):
                try:
                    packet.transport_mechanism = mesh_pb2.MeshPacket.TransportMechanism.TRANSPORT_MQTT
                except:
                    pass
            
            # Устанавливаем rx_time при получении пакета (как в firmware Router::handleReceived)
            if hasattr(packet, 'rx_time'):
                rx_time = get_valid_time(RTCQuality.FROM_NET)
                if rx_time > 0:
                    packet.rx_time = rx_time
            
            original_channel = packet.channel
            payload_type = packet.WhichOneof('payload_variant')
            
            if payload_type == 'decoded':
                if packet.channel != ch.index:
                    packet.channel = ch.index
            elif payload_type == 'encrypted':
                pass  # Channel для encrypted остается как hash
            
            if self.node_db:
                try:
                    self.node_db.update_from(packet)
                    
                    if packet.WhichOneof('payload_variant') == 'decoded' and hasattr(packet.decoded, 'portnum'):
                        packet_from = getattr(packet, 'from', 0)
                        if packet_from:
                            if packet.decoded.portnum == portnums_pb2.PortNum.NODEINFO_APP:
                                try:
                                    user = mesh_pb2.User()
                                    user.ParseFromString(packet.decoded.payload)
                                    self.node_db.update_user(packet_from, user, ch.index)
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
            
            from_radio = mesh_pb2.FromRadio()
            from_radio.packet.CopyFrom(packet)
            
            serialized = from_radio.SerializeToString()
            framed = StreamAPI.add_framing(serialized)
            self.to_client_queue.put(framed)
        except Exception as e:
            error("MQTT", f"Ошибка обработки сообщения: {e}")
            import traceback
            traceback.print_exc()
    
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
                self.client.publish(topic, payload)
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
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.connected = False

