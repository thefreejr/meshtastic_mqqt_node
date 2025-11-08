"""
Управление подключением к MQTT брокеру
"""

import ssl
import time
import threading
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
                 on_message_callback: Optional[Callable[[Any, Any, Any], None]] = None,
                 auto_reconnect: bool = True) -> None:
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
        self.auto_reconnect = auto_reconnect
        self._reconnect_thread = None
        self._reconnect_stop = False
        self._reconnect_lock = threading.Lock()
        self._auth_failed = False  # Флаг ошибки авторизации (останавливает переподключение)
        self._auth_failed_callback = None  # Callback для обновления флага в родительском объекте
    
    def connect(self) -> bool:
        """
        Подключается к MQTT брокеру
        
        Returns:
            True если подключение успешно, False иначе
        """
        # Если была ошибка авторизации, не пытаемся подключаться
        # Пользователь должен изменить настройки MQTT через AdminMessage
        if self._auth_failed:
            debug("MQTT", "Skipping connection attempt: authentication failed. Please update MQTT settings via AdminMessage.")
            return False
        
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
        
        # Логируем учетные данные (без пароля) для отладки
        password_preview = f"{self.password[:2]}..." if self.password and len(self.password) > 2 else (self.password if self.password else "empty")
        debug("MQTT", f"Setting credentials: username='{self.username}', password='{password_preview}' (length={len(self.password) if self.password else 0})")
        
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
        
        # ВАЖНО: Устанавливаем keepalive (как в firmware - по умолчанию 60 секунд)
        # Это нужно для обнаружения разорванных соединений
        keepalive = 60
        
        try:
            self.client.connect(self.broker, self.port, keepalive)
            self.client.loop_start()
            # Ждем подключения или ошибки (максимум 3 секунды)
            for _ in range(30):  # 30 * 0.1 = 3 секунды
                time.sleep(0.1)
                if self.connected:
                    return True
                # Если флаг ошибки авторизации установился (в callback), сразу выходим
                if self._auth_failed:
                    break
                # Если произошла ошибка подключения, выходим
                if not self.client._thread or not self.client._thread.is_alive():
                    break
            # Если подключение не удалось, проверяем, не была ли это ошибка авторизации
            if not self.connected:
                # Проверяем, есть ли информация об ошибке в клиенте
                # (ошибка авторизации уже обработана в _on_connect callback)
                if self._auth_failed:
                    # Флаг уже установлен в callback, просто возвращаем False
                    return False
            return self.connected
        except Exception as e:
            error("MQTT", f"Connection error: {e}")
            if self.client:
                try:
                    self.client.loop_stop()
                except:
                    pass
            # Проверяем, не была ли это ошибка авторизации
            if "Not authorized" in str(e) or "authorized" in str(e).lower():
                self._auth_failed = True
                self._reconnect_stop = True
                # Обновляем флаг в родительском объекте (если есть callback)
                if self._auth_failed_callback:
                    self._auth_failed_callback()
                warn("MQTT", "Authentication failed (Not authorized). Auto-reconnect stopped. Please update MQTT settings via AdminMessage.")
            return False
    
    def _on_connect(self, client, userdata, flags, rc, properties=None, reasonCode=None):
        """Callback при подключении к MQTT"""
        # В MQTT v5 может быть reasonCode вместо rc
        result_code = reasonCode if reasonCode is not None else rc
        
        # Проверяем успешное подключение (0 = успех)
        if result_code == 0 or (isinstance(result_code, int) and result_code == 0):
            self.connected = True
            # Останавливаем автоматическое переподключение, так как мы подключены
            self._reconnect_stop = True
            # Сбрасываем флаг ошибки авторизации при успешном подключении
            self._auth_failed = False
            # Также сбрасываем флаг в родительском MQTTClient, если есть доступ
            # (это делается через callback в MQTTClient.start)
            info("MQTT", f"Connected to {self.broker}:{self.port}")
            if self.on_connect_callback:
                self.on_connect_callback(client, userdata, flags, rc, properties, reasonCode)
        else:
            # Детальное логирование ошибок
            error_msg = self._get_error_message(result_code)
            error("MQTT", f"Connection error: {error_msg} (code: {result_code})")
            self.connected = False
            
            # Если ошибка "Not authorized" (код 5 или 135), останавливаем переподключение
            # Пользователь должен изменить настройки MQTT через AdminMessage
            is_auth_error = False
            if isinstance(result_code, int):
                is_auth_error = (result_code == 5 or result_code == 135)
            elif hasattr(result_code, 'value'):
                is_auth_error = (result_code.value == 5 or result_code.value == 135)
            elif "Not authorized" in error_msg or "Not authorized" in str(result_code):
                is_auth_error = True
            
            if is_auth_error:
                self._auth_failed = True
                self._reconnect_stop = True
                # Обновляем флаг в родительском объекте (если есть callback)
                if self._auth_failed_callback:
                    self._auth_failed_callback()
                warn("MQTT", "Authentication failed (Not authorized). Auto-reconnect stopped. Please update MQTT settings via AdminMessage.")
    
    def _on_disconnect(self, client, userdata, rc, properties=None, reasonCode=None):
        """Callback при отключении от MQTT"""
        # ВАЖНО: В paho-mqtt v2 может быть DisconnectFlags при подключении - это не ошибка
        # Проверяем это ПЕРВЫМ делом, до установки self.connected = False
        # Проверяем разными способами, так как структура может отличаться
        is_disconnect_flags = False
        if properties is not None:
            # Проверяем по имени класса
            if hasattr(properties, '__class__') and properties.__class__.__name__ == 'DisconnectFlags':
                is_disconnect_flags = True
            # Проверяем по строковому представлению
            elif 'DisconnectFlags' in str(type(properties)):
                is_disconnect_flags = True
            # Проверяем по атрибутам (если есть is_disconnect_packet_from_server)
            elif hasattr(properties, 'is_disconnect_packet_from_server'):
                is_disconnect_flags = True
        
        if is_disconnect_flags:
            # Это не ошибка отключения, а просто флаги - игнорируем полностью
            debug("MQTT", f"DisconnectFlags received (not an error, ignoring): {properties}")
            return
        
        # Только если это реальное отключение, устанавливаем флаг
        self.connected = False
        
        # В MQTT v5 может быть reasonCode в properties или как отдельный параметр
        # Если properties - это объект с reasonCode, извлекаем его
        if properties is not None:
            if hasattr(properties, 'reasonCode'):
                result_code = properties.reasonCode
            elif isinstance(properties, dict) and 'reasonCode' in properties:
                result_code = properties['reasonCode']
            elif hasattr(properties, '__class__'):
                # Уже проверили DisconnectFlags выше, здесь обрабатываем другие типы
                debug("MQTT", f"Disconnected from broker: Unknown error type: {properties.__class__.__name__} (code: {properties})")
                result_code = rc
            else:
                result_code = rc
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
        
        # ВАЖНО: Автоматическое переподключение (как в firmware MQTT::runOnce - проверяет loop() и переподключается)
        # НЕ переподключаемся, если была ошибка авторизации (пользователь должен изменить настройки)
        # Запускаем переподключение в отдельном потоке, чтобы не блокировать callback
        # ВАЖНО: Проверяем флаг ДО запуска auto-reconnect, так как он может быть установлен в _on_connect
        # (который вызывается перед _on_disconnect при ошибке подключения)
        if self.auto_reconnect and not self._reconnect_stop and not self._auth_failed:
            # Дополнительная проверка: если это была ошибка авторизации, не запускаем auto-reconnect
            # (флаг уже должен быть установлен в _on_connect, но проверяем на всякий случай)
            if result_code is not None:
                is_auth_error = False
                if isinstance(result_code, int):
                    is_auth_error = (result_code == 5 or result_code == 135)
                elif hasattr(result_code, 'value'):
                    is_auth_error = (result_code.value == 5 or result_code.value == 135)
                elif "Not authorized" in str(result_code):
                    is_auth_error = True
                
                if is_auth_error:
                    # Это ошибка авторизации - не запускаем auto-reconnect
                    # (флаг уже должен быть установлен в _on_connect)
                    debug("MQTT", "Disconnect due to auth error, skipping auto-reconnect")
                    return
            
            self._start_auto_reconnect()
        
        if self.on_disconnect_callback:
            self.on_disconnect_callback(client, userdata, rc, properties, reasonCode)
    
    def _start_auto_reconnect(self):
        """Запускает автоматическое переподключение в отдельном потоке"""
        with self._reconnect_lock:
            if self._reconnect_thread and self._reconnect_thread.is_alive():
                # Переподключение уже запущено
                return
            
            self._reconnect_stop = False
            self._reconnect_thread = threading.Thread(target=self._auto_reconnect_loop, daemon=True)
            self._reconnect_thread.start()
    
    def _auto_reconnect_loop(self):
        """Цикл автоматического переподключения (как в firmware MQTT::runOnce)"""
        import time
        reconnect_delay = 5  # Начальная задержка 5 секунд
        max_delay = 30  # Максимальная задержка 30 секунд (как в firmware)
        
        # Не запускаем переподключение, если была ошибка авторизации
        if self._auth_failed:
            warn("MQTT", "Auto-reconnect stopped due to authentication failure. Update MQTT settings to retry.")
            return
        
        while not self._reconnect_stop and not self._auth_failed:
            if self.connected:
                # Уже подключены - выходим
                break
            
            # Проверяем флаг перед каждой попыткой (может измениться во время ожидания)
            if self._auth_failed:
                warn("MQTT", "Auto-reconnect stopped: authentication failed. Update MQTT settings to retry.")
                break
            
            # Пытаемся переподключиться
            info("MQTT", f"Attempting to reconnect to {self.broker}:{self.port} (delay: {reconnect_delay}s)")
            if self.reconnect():
                info("MQTT", f"Successfully reconnected to {self.broker}:{self.port}")
                break
            else:
                # Проверяем, не была ли это ошибка авторизации
                if self._auth_failed:
                    warn("MQTT", "Reconnection stopped: authentication failed. Update MQTT settings to retry.")
                    break
                # Увеличиваем задержку для следующей попытки (экспоненциальный backoff)
                reconnect_delay = min(reconnect_delay * 2, max_delay)
                info("MQTT", f"Reconnection failed, will retry in {reconnect_delay}s")
            
            # Ждем перед следующей попыткой
            for _ in range(reconnect_delay * 10):  # Проверяем каждые 0.1 секунды
                if self._reconnect_stop or self._auth_failed:
                    break
                time.sleep(0.1)
    
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
        # Останавливаем автоматическое переподключение
        self._reconnect_stop = True
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=2)
        
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
        Переподключается к MQTT брокеру (как в firmware MQTT::reconnect)
        
        Returns:
            True если переподключение успешно, False иначе
        """
        # Если была ошибка авторизации, не пытаемся переподключаться
        if self._auth_failed:
            debug("MQTT", "Skipping reconnect: authentication failed. Please update MQTT settings via AdminMessage.")
            return False
        
        # Останавливаем текущее соединение, но не останавливаем автоматическое переподключение
        old_reconnect_stop = self._reconnect_stop
        self._reconnect_stop = True  # Временно останавливаем, чтобы не создавать дубликаты
        
        if self.client:
            try:
                # Останавливаем loop перед disconnect
                try:
                    if hasattr(self.client, '_thread') and self.client._thread and self.client._thread.is_alive():
                        self.client.loop_stop()
                        time.sleep(0.2)
                except:
                    pass
                # Отключаемся от брокера
                try:
                    self.client.disconnect()
                except:
                    pass
            except Exception as e:
                debug("MQTT", f"Error during disconnect before reconnect: {e}")
            finally:
                self.connected = False
        
        # Восстанавливаем флаг автоматического переподключения
        self._reconnect_stop = old_reconnect_stop
        
        # Ждем перед переподключением
        time.sleep(1)
        
        # Переподключаемся
        return self.connect()
    
    def get_client(self) -> Optional[mqtt.Client]:
        """Возвращает объект paho.mqtt.client"""
        return self.client

