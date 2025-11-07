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

from ..utils.logger import info, error, warn


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
        self.client.username_pw_set(self.username, self.password)
        
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
            error("MQTT", f"Ошибка подключения: {e}")
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
            info("MQTT", f"Подключен к {self.broker}:{self.port}")
            if self.on_connect_callback:
                self.on_connect_callback(client, userdata, flags, rc, properties, reasonCode)
        else:
            # Детальное логирование ошибок
            error_msg = self._get_error_message(result_code)
            error("MQTT", f"❌ Ошибка подключения: {error_msg} (код: {result_code})")
            self.connected = False
    
    def _on_disconnect(self, client, userdata, rc, properties=None, reasonCode=None):
        """Callback при отключении от MQTT"""
        self.connected = False
        
        # В MQTT v5 может быть reasonCode вместо rc
        result_code = reasonCode if reasonCode is not None else rc
        
        if result_code == 0 or (isinstance(result_code, int) and result_code == 0):
            info("MQTT", "Отключен от брокера (нормальное отключение)")
        else:
            error_msg = self._get_error_message(result_code)
            warn("MQTT", f"⚠️  Отключен от брокера: {error_msg} (код: {result_code})")
        
        if self.on_disconnect_callback:
            self.on_disconnect_callback(client, userdata, rc, properties, reasonCode)
    
    def _get_error_message(self, code: Any) -> str:
        """Возвращает текстовое описание ошибки MQTT"""
        # MQTT v3.1.1 коды ошибок
        mqtt_errors = {
            0: "Успешно",
            1: "Неправильная версия протокола",
            2: "Неверный идентификатор клиента",
            3: "Сервер недоступен",
            4: "Неверное имя пользователя или пароль",
            5: "Не авторизован",
        }
        
        # Если код - строка (как в некоторых версиях paho-mqtt)
        if isinstance(code, str):
            return code
        
        # Если код - число
        if isinstance(code, int):
            return mqtt_errors.get(code, f"Неизвестная ошибка (код: {code})")
        
        # Если это объект с атрибутами (MQTT v5)
        if hasattr(code, 'name'):
            return code.name
        if hasattr(code, 'value'):
            return str(code.value)
        
        return str(code)
    
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

