"""
TCP сервер для подключения meshtastic python CLI через StreamAPI
"""

import queue
import random
import select
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

from ..config import MAX_NUM_CHANNELS, START1, START2, HEADER_LEN, MAX_TO_FROM_RADIO_SIZE, DEFAULT_HOP_LIMIT, HOP_MAX, DEFAULT_MQTT_ADDRESS, DEFAULT_MQTT_USERNAME, DEFAULT_MQTT_PASSWORD, DEFAULT_MQTT_ROOT
from ..utils.logger import log, debug, info, warn, error, LogLevel
from ..mesh.config_storage import NodeConfig
from ..protocol.stream_api import StreamAPI, StreamAPIStateMachine
from ..mqtt.client import MQTTClient
from ..mesh.channels import Channels
from ..mesh.node_db import NodeDB
from ..mesh.rtc import RTCQuality, get_valid_time
from ..tcp.session import TCPConnectionSession


class TCPServer:
    """TCP сервер для подключения meshtastic python CLI через StreamAPI (мультисессионная архитектура)"""
    
    def __init__(self, port: int, 
                 default_mqtt_broker: str = DEFAULT_MQTT_ADDRESS,
                 default_mqtt_port: int = 1883,
                 default_mqtt_username: str = DEFAULT_MQTT_USERNAME,
                 default_mqtt_password: str = DEFAULT_MQTT_PASSWORD,
                 default_mqtt_root: str = DEFAULT_MQTT_ROOT) -> None:
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
        
        # Файл для сохранения маппингов (объединенный)
        # Хранится в config/ вместе с остальными конфигами
        config_dir = Path(__file__).parent.parent.parent / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        self.mapping_file = config_dir / "node_id_mapping.json"
        
        # Старые файлы для миграции
        self.device_id_mapping_file = config_dir / "device_id_mapping.json"
        self.ip_mapping_file = config_dir / "ip_to_node_id_mapping.json"
        
        # Загружаем сохраненные маппинги (с миграцией старых файлов)
        self._load_mappings()
    
    def _load_mappings(self) -> None:
        """Загружает маппинги из объединенного файла (с миграцией старых файлов)"""
        try:
            # Сначала пытаемся загрузить из объединенного файла
            if self.mapping_file.exists():
                with open(self.mapping_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Поддержка нового формата с секциями
                    if isinstance(data, dict) and 'device_id' in data and 'ip' in data:
                        self.device_id_to_node_id = data.get('device_id', {})
                        self.ip_to_node_id = data.get('ip', {})
                    else:
                        # Старый формат (плоский) - мигрируем
                        # Если это старый формат device_id_mapping.json (плоский словарь)
                        # или ip_to_node_id_mapping.json, то это уже обработано в миграции
                        self.device_id_to_node_id = data.get('device_id', {})
                        self.ip_to_node_id = data.get('ip', {})
                info("TCP", f"Loaded {len(self.device_id_to_node_id)} device_id -> node_id mappings and {len(self.ip_to_node_id)} IP -> node_id mappings")
            else:
                # Миграция: загружаем из старых файлов и объединяем
                self._migrate_old_mappings()
        except Exception as e:
            warn("TCP", f"Error loading mappings: {e}")
            self.device_id_to_node_id = {}
            self.ip_to_node_id = {}
            # Пытаемся мигрировать старые файлы
            self._migrate_old_mappings()
    
    def _migrate_old_mappings(self) -> None:
        """Мигрирует данные из старых отдельных файлов в объединенный"""
        migrated = False
        
        # Загружаем device_id маппинг из старого файла
        if self.device_id_mapping_file.exists():
            try:
                with open(self.device_id_mapping_file, 'r', encoding='utf-8') as f:
                    self.device_id_to_node_id = json.load(f)
                info("TCP", f"Migration: loaded {len(self.device_id_to_node_id)} device_id mappings from old file")
                migrated = True
            except Exception as e:
                warn("TCP", f"Error migrating device_id mapping: {e}")
                self.device_id_to_node_id = {}
        else:
            self.device_id_to_node_id = {}
        
        # Загружаем IP маппинг из старого файла
        if self.ip_mapping_file.exists():
            try:
                with open(self.ip_mapping_file, 'r', encoding='utf-8') as f:
                    self.ip_to_node_id = json.load(f)
                info("TCP", f"Migration: loaded {len(self.ip_to_node_id)} IP mappings from old file")
                migrated = True
            except Exception as e:
                warn("TCP", f"Error migrating IP mapping: {e}")
                self.ip_to_node_id = {}
        else:
            self.ip_to_node_id = {}
        
        # Сохраняем в новый объединенный файл
        if migrated:
            self._save_mappings()
            # Удаляем старые файлы после успешной миграции
            try:
                if self.device_id_mapping_file.exists():
                    self.device_id_mapping_file.unlink()
                    debug("TCP", "Removed old file device_id_mapping.json")
                if self.ip_mapping_file.exists():
                    self.ip_mapping_file.unlink()
                    debug("TCP", "Removed old file ip_to_node_id_mapping.json")
            except Exception as e:
                warn("TCP", f"Error removing old files: {e}")
    
    def _save_mappings(self) -> None:
        """Сохраняет все маппинги в объединенный файл"""
        try:
            data = {
                'device_id': self.device_id_to_node_id,
                'ip': self.ip_to_node_id
            }
            with open(self.mapping_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            debug("TCP", f"Saved {len(self.device_id_to_node_id)} device_id -> node_id mappings and {len(self.ip_to_node_id)} IP -> node_id mappings")
        except Exception as e:
            warn("TCP", f"Error saving mappings: {e}")
    
    def _save_device_id_mapping(self) -> None:
        """Сохраняет маппинг device_id -> node_id (обертка для совместимости)"""
        self._save_mappings()
    
    def _save_ip_mapping(self) -> None:
        """Сохраняет маппинг IP -> node_id (обертка для совместимости)"""
        self._save_mappings()
    
    def start(self) -> None:
        """Запускает TCP сервер"""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(('0.0.0.0', self.port))
        self.server_socket.listen(5)
        self.running = True
        
        info("TCP", f"Server started on port {self.port}")
        
        while self.running:
            try:
                client_socket, client_address = self.server_socket.accept()
                info("TCP", f"Connection from {client_address[0]}:{client_address[1]}")
                
                # Генерируем или получаем node_id для этого клиента
                # Сначала проверяем IP -> node_id маппинг (временная идентификация)
                # Затем, после получения device_id из MyInfo, обновим маппинг device_id -> node_id
                import hashlib
                
                client_ip = client_address[0]
                
                # Проверяем, есть ли уже node_id для этого IP
                node_id = None
                if client_ip in self.ip_to_node_id:
                    node_id = self.ip_to_node_id[client_ip]
                    info("TCP", f"Found node_id for IP {client_ip}: {node_id}")
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
                    
                    # Сохраняем маппинг IP -> node_id (сохраняется между перезапусками)
                    self.ip_to_node_id[client_ip] = node_id
                    self._save_ip_mapping()
                    info("TCP", f"Generated node_id for client {client_address[0]}:{client_address[1]}: {node_id}")
                
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
                # Клиент создается только если MQTT включен в module_config
                if session.config_storage.module_config.mqtt.enabled:
                    session.get_or_create_mqtt_client(
                        default_broker=self.default_mqtt_broker,
                        default_port=self.default_mqtt_port,
                        default_username=self.default_mqtt_username,
                        default_password=self.default_mqtt_password,
                        default_root=self.default_mqtt_root
                    )
                else:
                    debug("MQTT", f"MQTT disabled in module_config for session {session.node_id}, client not created")
                
                # Запускаем обработку клиента в отдельном потоке
                thread = threading.Thread(
                    target=self._handle_client_session,
                    args=(session,),
                    daemon=True
                )
                thread.start()
            except Exception as e:
                if self.running:
                    error("TCP", f"Error accepting connection: {e}")
    
    def stop(self) -> None:
        """Останавливает TCP сервер"""
        self.running = False
        
        # Закрываем все активные сессии ПЕРЕД закрытием сокета
        with self.sessions_lock:
            sessions_to_close = list(self.active_sessions.values())
            self.active_sessions.clear()
        
        # Закрываем сессии вне блокировки, чтобы избежать deadlock
        for session in sessions_to_close:
            try:
                session.close()
            except Exception as e:
                warn("TCP", f"Error closing session: {e}")
        
        # Закрываем сокет сервера
        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass
        
        # Даем время на завершение всех потоков
        import time
        time.sleep(0.5)
        
        info("TCP", "Server stopped")
    
    def _handle_client_session(self, session: TCPConnectionSession) -> None:
        """
        Обрабатывает подключение TCP клиента через сессию (как в firmware StreamAPI::runOncePart)
        Использует неблокирующий режим с адаптивным polling (5-250ms)
        """
        # Настраиваем сокет для долгоживущих соединений (как в firmware и Android клиенте)
        try:
            # Включаем keepalive, чтобы соединение не закрывалось из-за отсутствия активности
            session.client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # TCP_NODELAY - отключает алгоритм Nagle для уменьшения задержки (как в Android клиенте)
            session.client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # ВАЖНО: Переводим в неблокирующий режим (как в firmware)
            session.client_socket.setblocking(False)
        except Exception as e:
            debug("TCP", f"[{session._log_prefix()}] Error setting socket options: {e}")
            # Если не удалось установить опции, продолжаем
            try:
                session.client_socket.setblocking(False)
            except:
                pass
        
        # Создаем state machine для парсинга (как в firmware)
        state_machine = StreamAPIStateMachine(session._handle_to_radio)
        
        # Константы (как в firmware)
        SERIAL_CONNECTION_TIMEOUT_MS = 15 * 60 * 1000  # 15 минут (как в firmware SerialModule)
        POLL_INTERVAL_RECENT_MS = 5  # 5ms если недавно были данные (как в firmware)
        POLL_INTERVAL_IDLE_MS = 250  # 250ms если нет активности (как в firmware)
        RECENT_RX_THRESHOLD_MS = 2000  # 2 секунды для определения "недавно" (как в firmware)
        
        try:
            last_position_check = 0
            last_keepalive_send = 0
            # Android клиент закрывает соединение после 90 секунд неактивности
            # Отправляем данные каждые 60 секунд, чтобы соединение оставалось активным
            KEEPALIVE_INTERVAL = 60.0  # секунд
            
            while self.running:
                current_time = time.time()
                current_time_ms = int(current_time * 1000)
                
                # ВАЖНО: Сначала читаем из сокета, затем отправляем пакеты из MQTT
                # (как в firmware StreamAPI::runOncePart - readStream() затем writeStream())
                delay_ms = self._read_stream(session, state_machine, current_time_ms, RECENT_RX_THRESHOLD_MS)
                
                # Отправляем пакеты из MQTT (как в firmware writeStream())
                self._write_stream(session)
                
                # Проверяем таймаут соединения (как в firmware checkConnectionTimeout())
                if self._check_connection_timeout(session, state_machine, current_time_ms, SERIAL_CONNECTION_TIMEOUT_MS):
                    break
                
                # Периодическая отправка позиции (как в firmware PositionModule::runOnce)
                # Проверяем каждые 5 секунд (как RUNONCE_INTERVAL в firmware)
                if current_time - last_position_check >= 5.0:
                    last_position_check = current_time
                    try:
                        session._run_position_broadcast()
                    except Exception as e:
                        debug("TCP", f"[{session._log_prefix()}] Error running position broadcast: {e}")
                    
                    # Периодическая отправка NodeInfo (как в firmware NodeInfoModule::runOnce)
                    try:
                        session._run_nodeinfo_broadcast()
                    except Exception as e:
                        debug("TCP", f"[{session._log_prefix()}] Error running NodeInfo broadcast: {e}")
                
                # Периодическая отправка телеметрии через TCP для поддержания активности соединения
                # (Android клиент закрывает соединение после 90 секунд неактивности)
                if current_time - last_keepalive_send >= KEEPALIVE_INTERVAL:
                    last_keepalive_send = current_time
                    try:
                        session._send_telemetry_keepalive()
                    except Exception as e:
                        debug("TCP", f"[{session._log_prefix()}] Error sending keepalive: {e}")
                
                # Адаптивный polling (как в firmware)
                # Если delay_ms = 0, продолжаем немедленно, иначе ждем
                if delay_ms > 0:
                    # Используем select для неблокирующего ожидания (более эффективно чем time.sleep)
                    ready, _, _ = select.select([session.client_socket], [], [], delay_ms / 1000.0)
                    if not ready:
                        # Таймаут - продолжаем цикл
                        continue
        
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            # Соединение разорвано - закрываем сессию
            error("TCP", f"[{session._log_prefix()}] Connection broken: {e}")
        except Exception as e:
            error("TCP", f"[{session._log_prefix()}] Error processing client: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Удаляем сессию из активных ПЕРЕД закрытием
            with self.sessions_lock:
                if session.client_address in self.active_sessions:
                    del self.active_sessions[session.client_address]
            
            # Закрываем сессию (это закроет сокет и остановит MQTT клиент)
            session.close()
            info("TCP", f"[{session._log_prefix()}] Client {session.client_address[0]}:{session.client_address[1]} disconnected")
    
    def _read_stream(self, session: TCPConnectionSession, state_machine: StreamAPIStateMachine, 
                     current_time_ms: int, recent_threshold_ms: int) -> int:
        """
        Читает данные из потока (как в firmware StreamAPI::readStream)
        
        Returns:
            Задержка до следующего вызова в миллисекундах (0 = немедленно, 5-250ms = адаптивная задержка)
        """
        # Константы polling (как в firmware)
        POLL_INTERVAL_RECENT_MS = 5  # 5ms если недавно были данные
        POLL_INTERVAL_IDLE_MS = 250  # 250ms если нет активности
        
        try:
            # Пытаемся прочитать данные (неблокирующий режим)
            data = session.client_socket.recv(4096)
            
            if not data:
                # Клиент закрыл соединение (recv вернул пустые данные)
                debug("TCP", f"[{session._log_prefix()}] Client closed connection (recv returned empty)")
                raise ConnectionResetError("Client closed connection")
            
            # Обрабатываем данные через state machine (как в firmware handleRecStream)
            state_machine.handle_rec_stream(data)
            
            # Были данные - предполагаем, что могут быть еще, возвращаем 0 для немедленного продолжения
            return 0
            
        except BlockingIOError:
            # Нет доступных данных (неблокирующий режим) - это нормально
            # Адаптивный polling: если недавно были данные, проверяем часто, иначе редко
            last_rx_ms = state_machine.get_last_rx_msec()
            if last_rx_ms > 0:
                time_since_last = current_time_ms - last_rx_ms
                if time_since_last < recent_threshold_ms:
                    # Недавно были данные - проверяем часто (5ms)
                    return POLL_INTERVAL_RECENT_MS
                else:
                    # Давно не было данных - проверяем редко (250ms)
                    return POLL_INTERVAL_IDLE_MS
            else:
                # Никогда не было данных - проверяем редко
                return POLL_INTERVAL_IDLE_MS
                
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            # Соединение разорвано - пробрасываем наверх
            raise
        except Exception as e:
            # Другие ошибки - логируем, но не закрываем соединение
            error("TCP", f"[{session._log_prefix()}] Error reading from stream: {e}")
            return POLL_INTERVAL_IDLE_MS
    
    def _write_stream(self, session: TCPConnectionSession) -> None:
        """
        Отправляет пакеты из очереди (как в firmware StreamAPI::writeStream)
        Отправляет все доступные пакеты за один вызов
        """
        if not session.mqtt_client or not hasattr(session.mqtt_client, 'to_client_queue'):
            return
        
        try:
            # Отправляем все доступные пакеты (как в firmware - do { ... } while (len))
            packets_sent = 0
            max_packets_per_iteration = 50  # Увеличиваем лимит, так как неблокирующий режим
            
            while not session.mqtt_client.to_client_queue.empty() and packets_sent < max_packets_per_iteration:
                try:
                    response = session.mqtt_client.to_client_queue.get_nowait()
                    
                    # Обрабатываем пакет перед отправкой (для обновления NodeDB)
                    try:
                        from_radio_data = StreamAPI.remove_framing(response)
                        if from_radio_data:
                            from_radio = mesh_pb2.FromRadio()
                            from_radio.ParseFromString(from_radio_data)
                            if from_radio.HasField('packet'):
                                session._handle_mqtt_packet(from_radio.packet)
                    except:
                        pass
                    
                    # Отправляем пакет
                    try:
                        session.client_socket.send(response)
                        packets_sent += 1
                    except BlockingIOError:
                        # Буфер отправки переполнен - это нормально, попробуем в следующий раз
                        # Возвращаем пакет в очередь
                        try:
                            session.mqtt_client.to_client_queue.put_nowait(response)
                        except queue.Full:
                            warn("TCP", f"[{session._log_prefix()}] MQTT queue full, dropping packet")
                        break
                    except (BrokenPipeError, ConnectionResetError, OSError) as e:
                        # TCP соединение разорвано - пробрасываем наверх
                        raise
                        
                except queue.Empty:
                    break
                    
        except (AttributeError, queue.Empty):
            pass
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Ошибки разрыва TCP соединения - пробрасываем наверх
            raise
        except Exception as e:
            # Ошибки MQTT (не связанные с TCP) не должны закрывать TCP сессию
            debug("TCP", f"[{session._log_prefix()}] Error processing MQTT packets (MQTT issue, not TCP): {e}")
    
    def _check_connection_timeout(self, session: TCPConnectionSession, state_machine: StreamAPIStateMachine,
                                   current_time_ms: int, timeout_ms: int) -> bool:
        """
        Проверяет таймаут соединения (как в firmware PhoneAPI::checkConnectionTimeout)
        
        Returns:
            True если соединение должно быть закрыто из-за таймаута
        """
        last_rx_ms = state_machine.get_last_rx_msec()
        
        if last_rx_ms > 0:
            time_since_last = current_time_ms - last_rx_ms
            if time_since_last > timeout_ms:
                # Таймаут соединения - не было данных слишком долго
                info("TCP", f"[{session._log_prefix()}] Connection timeout ({time_since_last}ms > {timeout_ms}ms)")
                return True
        
        return False

