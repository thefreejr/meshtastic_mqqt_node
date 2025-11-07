"""
Управление подключением к MQTT брокеру
"""

import ssl
import time
from typing import Optional, Callable, Any

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Ошибка: Установите paho-mqtt: pip install paho-mqtt")
    raise

from ..utils.logger import info, error, warn, debug


class MQTTConnection:
    """Управление подключением к MQTT брокеру"""
    
    def __init__(self, broker: str, port: int, username: str, password: str, 
                 node_id: str,
                 on_connect_callback: Optional[Callable[[Any, Any, Any, int, Any, Any], None]] = None,
                 on_disconnect_callback: Optional[Callable[[Any, Any, int, Any, Any], None]] = None,
                 on_message_callback: Optional[Callable[[Any, Any, Any], None]] = None) -> None:
        """
        Инициализирует MQTT подключение
        
        Args:
            broker: Адрес MQTT брокера
            port: Порт MQTT брокера
            username: Имя пользователя
            password: Пароль
            node_id: Node ID для client_id
            on_connect_callback: Callback при подключении
            on_disconnect_callback: Callback при отключении
            on_message_callback: Callback при получении сообщения
        """
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.node_id = node_id
        self.client: Optional[mqtt.Client] = None
        self.connected = False
        self.on_connect_callback = on_connect_callback
        self.on_disconnect_callback = on_disconnect_callback
        self.on_message_callback = on_message_callback
    
    def connect(self) -> bool:
        """
        Подключается к MQTT брокеру
        
        Returns:
            True если подключение успешно, False иначе
        """
        try:
            # Создаем клиент с правильной версией API
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
        
        # Устанавливаем учетные данные
        if not self.username:
            warn("MQTT", "Username is empty, connection may fail")
        if not self.password:
            warn("MQTT", "Password is empty, connection may fail")
        self.client.username_pw_set(self.username, self.password)
        debug("MQTT", f"Setting credentials: username='{self.username}', password_length={len(self.password) if self.password else 0}")
        
        # Устанавливаем callbacks
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        if self.on_message_callback:
            self.client.on_message = self.on_message_callback
        
        # Настраиваем TLS если порт 8883
        if self.port == 8883:
            self.client.tls_set(cert_reqs=ssl.CERT_NONE)
            self.client.tls_insecure_set(True)
        
        try:
            self.client.connect(self.broker, self.port, 60)
            self.client.loop_start()
            # Ждем подключения или ошибки (максимум 3 секунды)
            for _ in range(30):  # 30 * 0.1 = 3 секунды
                time.sleep(0.1)
                if self.connected:
                    return True
                # Если произошла ошибка подключения, выходим
                if not self.client._thread or not self.client._thread.is_alive():
                    break
            return self.connected
        except Exception as e:
            error("MQTT", f"Connection error: {e}")
            if self.client:
                try:
                    self.client.loop_stop()
                except:
                    pass
            return False
    
    def _on_connect(self, client, userdata, flags, rc, properties=None, reasonCode=None):
        """Callback при подключении к MQTT"""
        # В MQTT v5 может быть reasonCode вместо rc
        result_code = reasonCode if reasonCode is not None else rc
        
        # Проверяем успешное подключение (0 = успех)
        if result_code == 0 or (isinstance(result_code, int) and result_code == 0):
            self.connected = True
            info("MQTT", f"Connected to {self.broker}:{self.port}")
            if self.on_connect_callback:
                self.on_connect_callback(client, userdata, flags, rc, properties, reasonCode)
        else:
            # Детальное логирование ошибок
            error_msg = self._get_error_message(result_code)
            error("MQTT", f"Connection error: {error_msg} (code: {result_code})")
            self.connected = False
    
    def _on_disconnect(self, client, userdata, rc, properties=None, reasonCode=None):
        """Callback при отключении от MQTT"""
        self.connected = False
        
        # В MQTT v5 может быть reasonCode в properties или как отдельный параметр
        # Если properties - это объект с reasonCode, извлекаем его
        if properties is not None and hasattr(properties, 'reasonCode'):
            result_code = properties.reasonCode
        elif reasonCode is not None:
            result_code = reasonCode
        else:
            result_code = rc
        
        # Обрабатываем случаи, когда код может быть None или неожиданным типом
        if result_code is None:
            debug("MQTT", "Disconnected from broker (reason code not provided)")
        elif result_code == 0 or (isinstance(result_code, int) and result_code == 0):
            info("MQTT", "Disconnected from broker (normal disconnect)")
        else:
            error_msg = self._get_error_message(result_code)
            warn("MQTT", f"Disconnected from broker: {error_msg} (code: {result_code})")
        
        if self.on_disconnect_callback:
            self.on_disconnect_callback(client, userdata, rc, properties, reasonCode)
    
    def _get_error_message(self, code: Any) -> str:
        """Возвращает текстовое описание ошибки MQTT"""
        # Обрабатываем None и пустые значения
        if code is None:
            return "No reason code provided"
        
        # Обрабатываем пустые списки/кортежи
        if isinstance(code, (list, tuple)) and len(code) == 0:
            return "No reason code provided"
        
        # MQTT v3.1.1 коды ошибок
        mqtt_errors = {
            0: "Success",
            1: "Incorrect protocol version",
            2: "Invalid client identifier",
            3: "Server unavailable",
            4: "Bad username or password",
            5: "Not authorized",
        }
        
        # MQTT v5 reason codes (расширенные)
        mqtt_v5_errors = {
            128: "Unspecified error",
            129: "Malformed packet",
            130: "Protocol error",
            131: "Implementation specific error",
            132: "Unsupported protocol version",
            133: "Client identifier not valid",
            134: "Bad username or password",
            135: "Not authorized",
            136: "Server unavailable",
            137: "Server busy",
            138: "Banned",
            139: "Server shutting down",
            140: "Bad authentication method",
            141: "Keep alive timeout",
            142: "Session taken over",
            143: "Topic filter invalid",
            144: "Topic name invalid",
            145: "Packet identifier in use",
            146: "Packet identifier not found",
            147: "Receive maximum exceeded",
            148: "Topic alias invalid",
            149: "Packet too large",
            150: "Message rate too high",
            151: "Quota exceeded",
            152: "Administrative action",
            153: "Payload format invalid",
            154: "Retain not supported",
            155: "QoS not supported",
            156: "Use another server",
            157: "Server moved",
            158: "Shared subscriptions not supported",
            159: "Connection rate exceeded",
            160: "Maximum connect time",
            161: "Subscription identifiers not supported",
            162: "Wildcard subscriptions not supported",
        }
        
        # Если код - строка (как в некоторых версиях paho-mqtt)
        if isinstance(code, str):
            return code
        
        # Если код - число
        if isinstance(code, int):
            # Проверяем сначала стандартные коды, затем MQTT v5
            if code in mqtt_errors:
                return mqtt_errors[code]
            elif code in mqtt_v5_errors:
                return mqtt_v5_errors[code]
            else:
                return f"Unknown error (code: {code})"
        
        # Если это объект с атрибутами (MQTT v5)
        if hasattr(code, 'name'):
            return code.name
        if hasattr(code, 'value'):
            # Если value - число, проверяем в словарях
            if isinstance(code.value, int):
                if code.value in mqtt_errors:
                    return mqtt_errors[code.value]
                elif code.value in mqtt_v5_errors:
                    return mqtt_v5_errors[code.value]
            return str(code.value)
        
        # Если это объект Properties, пытаемся извлечь reasonCode
        if hasattr(code, 'reasonCode'):
            reason = code.reasonCode
            if isinstance(reason, int):
                if reason in mqtt_errors:
                    return mqtt_errors[reason]
                elif reason in mqtt_v5_errors:
                    return mqtt_v5_errors[reason]
            return str(reason)
        
        return f"Unknown error type: {type(code).__name__}"
    
    def disconnect(self) -> None:
        """Отключается от MQTT брокера"""
        if self.client:
            try:
                # Останавливаем loop перед disconnect для корректного завершения
                try:
                    if hasattr(self.client, '_thread') and self.client._thread and self.client._thread.is_alive():
                        self.client.loop_stop()
                        # Даем время на завершение потока
                        time.sleep(0.2)
                except:
                    pass
                # Отключаемся от брокера
                try:
                    self.client.disconnect()
                except:
                    pass
            except Exception as e:
                # Игнорируем ошибки при отключении
                pass
            finally:
                self.connected = False
    
    def is_connected(self) -> bool:
        """Проверяет, подключен ли клиент"""
        return self.connected
    
    def reconnect(self) -> bool:
        """
        Переподключается к MQTT брокеру
        
        Returns:
            True если переподключение успешно, False иначе
        """
        self.disconnect()
        time.sleep(1)
        return self.connect()
    
    def get_client(self) -> Optional[mqtt.Client]:
        """Возвращает объект paho.mqtt.client"""
        return self.client

