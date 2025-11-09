"""
Хранилище конфигурации узла (из firmware/src/mesh/NodeDB.cpp)
Читает дефолтные настройки из config/node_defaults.json
Также содержит статические настройки узла (NodeConfig) для отправки клиенту
"""

import base64
import json
from pathlib import Path
from typing import Any, Optional

from meshtastic import mesh_pb2
from meshtastic.protobuf import admin_pb2, config_pb2, module_config_pb2

from ..config_loader import ConfigLoader
from ..utils.logger import LogLevel

# Путь к файлу дефолтных настроек
NODE_DEFAULTS_FILE = Path(__file__).parent.parent.parent / "config" / "node_defaults.json"

# Используем единый загрузчик конфигурации
_config_loader = ConfigLoader.get_instance()

def _get_config_value(path: str, default: Any) -> Any:
    """Получает значение из конфигурации по пути"""
    return _config_loader.get_value(path, default)


def normalize_firmware_version(version: str) -> str:
    """
    Нормализует версию прошивки, убирая суффиксы и проверяя формат X.Y.Z
    
    Args:
        version: Версия прошивки (может содержать суффиксы типа .60ec05e)
        
    Returns:
        Нормализованная версия в формате X.Y.Z
    """
    if not version or not version.strip():
        return "2.7.0"
    
    version = version.strip()
    
    # Убираем суффиксы после точки (например, "2.6.11.60ec05e" -> "2.6.11")
    # Ищем паттерн X.Y.Z и останавливаемся на третьей цифре после точки
    import re
    match = re.match(r'^(\d+\.\d+\.\d+)', version)
    if match:
        normalized = match.group(1)
        # Проверяем, что версия не слишком старая (минимум 2.5.14)
        parts = [int(x) for x in normalized.split('.')]
        if len(parts) == 3:
            # Если версия меньше 2.5.14, используем дефолтную 2.7.0
            if (parts[0] < 2) or (parts[0] == 2 and parts[1] < 5) or (parts[0] == 2 and parts[1] == 5 and parts[2] < 14):
                return "2.7.0"
        return normalized
    
    # Если формат не распознан, возвращаем дефолтную версию
    return "2.7.0"


class NodeConfig:
    """Статические настройки узла для отправки клиенту (из config/project.yaml)"""
    
    # Имена узла
    USER_LONG_NAME = _get_config_value('node.user_long_name', "MQTT Node")
    USER_SHORT_NAME = _get_config_value('node.user_short_name', "MQTT")
    
    # Hardware модель
    HW_MODEL = _get_config_value('node.hw_model', "PORTDUINO")
    
    # Device Metrics
    DEVICE_METRICS_BATTERY_LEVEL = _get_config_value('node.device_metrics.battery_level', 101)
    DEVICE_METRICS_VOLTAGE = _get_config_value('node.device_metrics.voltage', 5.0)
    DEVICE_METRICS_CHANNEL_UTILIZATION = _get_config_value('node.device_metrics.channel_utilization', 0.0)
    DEVICE_METRICS_AIR_UTIL_TX = _get_config_value('node.device_metrics.air_util_tx', 0.0)
    
    # Metadata
    # ВАЖНО: Версия должна быть в формате X.Y.Z (без суффиксов), чтобы клиент мог правильно парсить
    # Минимальная версия для Android клиента: 2.5.14, абсолютный минимум: 2.3.15
    # Используем версию 2.7.0, которая выше минимальной и соответствует формату парсинга
    # Версия нормализуется автоматически (убираются суффиксы)
    _raw_firmware_version = _get_config_value('node.firmware_version', "2.7.0")
    FIRMWARE_VERSION = normalize_firmware_version(_raw_firmware_version)
    
    # Fallback node_num
    FALLBACK_NODE_NUM = _get_config_value('node.fallback_node_num', 0x12345678)


class ConfigStorage:
    """
    Хранилище конфигурации узла
    Загружает дефолтные настройки из config/node_defaults.json при инициализации
    Содержит protobuf конфигурацию (Config и ModuleConfig), которая может изменяться через Admin API
    """
    
    def __init__(self) -> None:
        # Инициализируем пустые protobuf объекты
        self.config = config_pb2.Config()
        self.module_config = module_config_pb2.ModuleConfig()
        
        # Загружаем дефолтные настройки из файла
        # Если файл не существует, создаем его с дефолтными значениями
        if not self._load_defaults_from_file():
            # Файл не существует - создаем дефолтные значения программно
            # и сохраняем в файл для следующего раза
            self._create_defaults()
            self._save_defaults_to_file()
    
    def get_config(self, config_type: int) -> Optional[config_pb2.Config]:
        """
        Возвращает конфигурацию указанного типа (как в firmware AdminModule::handleGetConfig)
        
        Args:
            config_type: Тип конфигурации (из AdminMessage.ConfigType)
            
        Returns:
            Config с заполненным соответствующим полем или None
        """
        # Создаем новый Config с только нужным полем (как в firmware)
        response = config_pb2.Config()
        
        # Определяем тип конфигурации и заполняем соответствующее поле
        if config_type == admin_pb2.AdminMessage.ConfigType.DEVICE_CONFIG:
            response.device.CopyFrom(self.config.device)
        elif config_type == admin_pb2.AdminMessage.ConfigType.POSITION_CONFIG:
            response.position.CopyFrom(self.config.position)
        elif config_type == admin_pb2.AdminMessage.ConfigType.POWER_CONFIG:
            response.power.CopyFrom(self.config.power)
            # ВАЖНО: Устанавливаем ls_secs явно (как в firmware PhoneAPI.cpp:321)
            # Это нужно для совместимости со старыми клиентами
            # Даже если внутри используется 0 для "use default", нужно отправить реальное значение
            # В firmware всегда устанавливается default_ls_secs, даже если значение уже есть
            # Используем значение по умолчанию (5 минут для обычного устройства, не роутера)
            default_ls_secs = 5 * 60
            if response.power.ls_secs == 0:
                response.power.ls_secs = default_ls_secs
        elif config_type == admin_pb2.AdminMessage.ConfigType.NETWORK_CONFIG:
            response.network.CopyFrom(self.config.network)
        elif config_type == admin_pb2.AdminMessage.ConfigType.DISPLAY_CONFIG:
            response.display.CopyFrom(self.config.display)
        elif config_type == admin_pb2.AdminMessage.ConfigType.LORA_CONFIG:
            response.lora.CopyFrom(self.config.lora)
        elif config_type == admin_pb2.AdminMessage.ConfigType.BLUETOOTH_CONFIG:
            response.bluetooth.CopyFrom(self.config.bluetooth)
        elif config_type == admin_pb2.AdminMessage.ConfigType.SECURITY_CONFIG:
            response.security.CopyFrom(self.config.security)
        elif config_type == admin_pb2.AdminMessage.ConfigType.SESSIONKEY_CONFIG:
            # SessionKey config - пустой (как в firmware)
            if self.config.HasField('sessionkey'):
                response.sessionkey.CopyFrom(self.config.sessionkey)
            else:
                # Создаем пустой объект, если его нет
                response.sessionkey.CopyFrom(config_pb2.Config.SessionkeyConfig())
        elif config_type == admin_pb2.AdminMessage.ConfigType.DEVICEUI_CONFIG:
            # DeviceUI config - пустой NOOP (как в firmware PhoneAPI.cpp:352-354)
            # Только устанавливаем which_payload_variant, без копирования содержимого
            if self.config.HasField('device_ui'):
                response.device_ui.CopyFrom(self.config.device_ui)
            else:
                # Создаем пустой объект, если его нет
                from meshtastic.protobuf import device_ui_pb2
                response.device_ui.CopyFrom(device_ui_pb2.DeviceUIConfig())
        else:
            return None
        
        return response
    
    def get_module_config(self, module_config_type: int) -> Optional[module_config_pb2.ModuleConfig]:
        """
        Возвращает конфигурацию модуля указанного типа (как в firmware AdminModule::handleGetModuleConfig)
        
        Args:
            module_config_type: Тип конфигурации модуля (из AdminMessage.ModuleConfigType)
            
        Returns:
            ModuleConfig с заполненным соответствующим полем или None
        """
        # Создаем новый ModuleConfig с только нужным полем (как в firmware)
        response = module_config_pb2.ModuleConfig()
        
        # Определяем тип конфигурации модуля и заполняем соответствующее поле
        if module_config_type == admin_pb2.AdminMessage.ModuleConfigType.MQTT_CONFIG:
            response.mqtt.CopyFrom(self.module_config.mqtt)
        elif module_config_type == admin_pb2.AdminMessage.ModuleConfigType.SERIAL_CONFIG:
            response.serial.CopyFrom(self.module_config.serial)
        elif module_config_type == admin_pb2.AdminMessage.ModuleConfigType.EXTNOTIF_CONFIG:
            response.external_notification.CopyFrom(self.module_config.external_notification)
        elif module_config_type == admin_pb2.AdminMessage.ModuleConfigType.STOREFORWARD_CONFIG:
            response.store_forward.CopyFrom(self.module_config.store_forward)
        elif module_config_type == admin_pb2.AdminMessage.ModuleConfigType.RANGETEST_CONFIG:
            response.range_test.CopyFrom(self.module_config.range_test)
        elif module_config_type == admin_pb2.AdminMessage.ModuleConfigType.TELEMETRY_CONFIG:
            response.telemetry.CopyFrom(self.module_config.telemetry)
        elif module_config_type == admin_pb2.AdminMessage.ModuleConfigType.CANNEDMSG_CONFIG:
            response.canned_message.CopyFrom(self.module_config.canned_message)
        elif module_config_type == admin_pb2.AdminMessage.ModuleConfigType.AUDIO_CONFIG:
            response.audio.CopyFrom(self.module_config.audio)
        elif module_config_type == admin_pb2.AdminMessage.ModuleConfigType.REMOTEHARDWARE_CONFIG:
            response.remote_hardware.CopyFrom(self.module_config.remote_hardware)
        elif module_config_type == admin_pb2.AdminMessage.ModuleConfigType.NEIGHBORINFO_CONFIG:
            response.neighbor_info.CopyFrom(self.module_config.neighbor_info)
        elif module_config_type == admin_pb2.AdminMessage.ModuleConfigType.AMBIENTLIGHTING_CONFIG:
            response.ambient_lighting.CopyFrom(self.module_config.ambient_lighting)
        elif module_config_type == admin_pb2.AdminMessage.ModuleConfigType.DETECTIONSENSOR_CONFIG:
            response.detection_sensor.CopyFrom(self.module_config.detection_sensor)
        elif module_config_type == admin_pb2.AdminMessage.ModuleConfigType.PAXCOUNTER_CONFIG:
            response.paxcounter.CopyFrom(self.module_config.paxcounter)
        else:
            return None
        
        return response
    
    def set_config(self, config: config_pb2.Config) -> None:
        """Устанавливает конфигурацию (как в firmware AdminModule::handleSetConfig)"""
        # Обновляем соответствующие поля в self.config
        if config.HasField('device'):
            self.config.device.CopyFrom(config.device)
        if config.HasField('position'):
            self.config.position.CopyFrom(config.position)
        if config.HasField('power'):
            self.config.power.CopyFrom(config.power)
        if config.HasField('network'):
            self.config.network.CopyFrom(config.network)
        if config.HasField('display'):
            self.config.display.CopyFrom(config.display)
        if config.HasField('lora'):
            self.config.lora.CopyFrom(config.lora)
        if config.HasField('bluetooth'):
            self.config.bluetooth.CopyFrom(config.bluetooth)
        if config.HasField('security'):
            self.config.security.CopyFrom(config.security)
    
    def set_module_config(self, module_config: module_config_pb2.ModuleConfig) -> None:
        """Устанавливает конфигурацию модуля (как в firmware AdminModule::handleSetModuleConfig)"""
        # Обновляем соответствующие поля в self.module_config
        if module_config.HasField('mqtt'):
            # Сохраняем текущее значение enabled (по умолчанию True)
            current_enabled = self.module_config.mqtt.enabled
            
            # Копируем весь mqtt config
            self.module_config.mqtt.CopyFrom(module_config.mqtt)
            
            # ВАЖНО: Для bool полей в protobuf нет HasField, поэтому проверяем через сравнение
            # Если enabled в запросе = False (дефолт для bool), но был True до этого,
            # это может означать, что поле не было установлено явно
            # Создаем временный config с дефолтными значениями для сравнения
            temp_default = module_config_pb2.ModuleConfig()
            # Если enabled в запросе равен дефолтному False, но был True до этого,
            # возможно, поле не было установлено явно - сохраняем текущее значение True
            # Но только если enabled действительно False (не был явно установлен в True)
            if (module_config.mqtt.enabled == temp_default.mqtt.enabled and 
                current_enabled and 
                not self.module_config.mqtt.enabled):
                # enabled не был явно установлен (остался дефолтным False), сохраняем текущее значение True
                self.module_config.mqtt.enabled = current_enabled
        if module_config.HasField('serial'):
            self.module_config.serial.CopyFrom(module_config.serial)
        if module_config.HasField('external_notification'):
            self.module_config.external_notification.CopyFrom(module_config.external_notification)
        if module_config.HasField('store_forward'):
            self.module_config.store_forward.CopyFrom(module_config.store_forward)
        if module_config.HasField('range_test'):
            self.module_config.range_test.CopyFrom(module_config.range_test)
        if module_config.HasField('telemetry'):
            self.module_config.telemetry.CopyFrom(module_config.telemetry)
        if module_config.HasField('canned_message'):
            self.module_config.canned_message.CopyFrom(module_config.canned_message)
        if module_config.HasField('audio'):
            self.module_config.audio.CopyFrom(module_config.audio)
        if module_config.HasField('remote_hardware'):
            self.module_config.remote_hardware.CopyFrom(module_config.remote_hardware)
        if module_config.HasField('neighbor_info'):
            self.module_config.neighbor_info.CopyFrom(module_config.neighbor_info)
        if module_config.HasField('ambient_lighting'):
            self.module_config.ambient_lighting.CopyFrom(module_config.ambient_lighting)
        if module_config.HasField('detection_sensor'):
            self.module_config.detection_sensor.CopyFrom(module_config.detection_sensor)
        if module_config.HasField('paxcounter'):
            self.module_config.paxcounter.CopyFrom(module_config.paxcounter)
    
    def _load_defaults_from_file(self) -> bool:
        """Загружает дефолтные настройки из config/node_defaults.json
        
        Returns:
            True если файл существует и загружен успешно с непустыми данными, False если файл не существует или содержит пустые данные
        """
        try:
            if not NODE_DEFAULTS_FILE.exists():
                return False
            
            with open(NODE_DEFAULTS_FILE, 'r', encoding='utf-8') as f:
                defaults = json.load(f)
            
            loaded_config = False
            loaded_module_config = False
            
            # Загружаем config, если он есть и не пустой
            if defaults.get('config'):
                if isinstance(defaults['config'], dict) and '_type' in defaults['config'] and '_data' in defaults['config']:
                    data = defaults['config']['_data']
                    # Проверяем, что данные не пустые (минимум несколько символов)
                    if data and len(data) > 4:
                        try:
                            serialized = base64.b64decode(data)
                            loaded_config_pb = config_pb2.Config()
                            loaded_config_pb.ParseFromString(serialized)
                            self.config.CopyFrom(loaded_config_pb)
                            loaded_config = True
                        except Exception:
                            pass
            
            # Загружаем module_config, если он есть и не пустой
            if defaults.get('module_config'):
                if isinstance(defaults['module_config'], dict) and '_type' in defaults['module_config'] and '_data' in defaults['module_config']:
                    data = defaults['module_config']['_data']
                    # Проверяем, что данные не пустые (минимум несколько символов)
                    if data and len(data) > 4:
                        try:
                            serialized = base64.b64decode(data)
                            loaded_module_config_pb = module_config_pb2.ModuleConfig()
                            loaded_module_config_pb.ParseFromString(serialized)
                            self.module_config.CopyFrom(loaded_module_config_pb)
                            loaded_module_config = True
                        except Exception:
                            pass
            
            # Возвращаем True если хотя бы один конфиг загружен успешно
            # (config может быть пустым, но module_config должен быть загружен)
            return loaded_config or loaded_module_config
        except Exception as e:
            # Если ошибка загрузки, возвращаем False
            return False
    
    def _create_defaults(self):
        """Создает дефолтные настройки программно (используется только при первом запуске)"""
        # Device Config
        self.config.device.role = config_pb2.Config.DeviceConfig.Role.CLIENT
        self.config.device.rebroadcast_mode = config_pb2.Config.DeviceConfig.RebroadcastMode.ALL
        
        # LoRa Config
        self.config.lora.tx_enabled = True
        self.config.lora.sx126x_rx_boosted_gain = True
        self.config.lora.override_duty_cycle = False
        self.config.lora.config_ok_to_mqtt = False
        self.config.lora.modem_preset = config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST
        self.config.lora.region = config_pb2.Config.LoRaConfig.RegionCode.UNSET
        
        # Position Config
        self.config.position.gps_update_interval = 2 * 60
        self.config.position.position_broadcast_secs = 15 * 60
        self.config.position.gps_mode = config_pb2.Config.PositionConfig.GpsMode.ENABLED
        
        # Power Config
        self.config.power.ls_secs = 5 * 60
        self.config.power.min_wake_secs = 10
        self.config.power.sds_secs = 0xFFFFFFFF
        self.config.power.wait_bluetooth_secs = 60
        self.config.power.is_power_saving = False
        
        # Display Config
        self.config.display.screen_on_secs = 60 * 10
        self.config.display.displaymode = config_pb2.Config.DisplayConfig.DisplayMode.DEFAULT
        
        # Network Config
        self.config.network.wifi_enabled = False
        self.config.network.eth_enabled = False
        
        # Bluetooth Config
        self.config.bluetooth.enabled = False
        
        # Module Config - MQTT
        self.module_config.mqtt.enabled = True
        self.module_config.mqtt.address = "mqtt.meshtastic.org"
        self.module_config.mqtt.username = "meshdev"
        self.module_config.mqtt.password = "large4cats"
        self.module_config.mqtt.root = "msh"
        self.module_config.mqtt.encryption_enabled = True
        self.module_config.mqtt.tls_enabled = False
        
        # Module Config - Serial
        self.module_config.serial.enabled = False
        self.module_config.serial.timeout = 5000
        self.module_config.serial.mode = 0
        
        # Module Config - Other
        self.module_config.external_notification.enabled = False
        self.module_config.store_forward.enabled = False
        self.module_config.range_test.enabled = False
        self.module_config.telemetry.device_update_interval = 30 * 60
        self.module_config.telemetry.environment_update_interval = 60 * 60
        self.module_config.telemetry.environment_measurement_enabled = False
        self.module_config.canned_message.enabled = False
        self.module_config.audio.codec2_enabled = False
        self.module_config.audio.ptt_pin = 0
        self.module_config.audio.bitrate = 3200
        self.module_config.remote_hardware.enabled = False
        self.module_config.neighbor_info.enabled = False
        self.module_config.neighbor_info.update_interval = 6 * 60 * 60
        self.module_config.detection_sensor.enabled = False
        self.module_config.paxcounter.enabled = False
    
    def _save_defaults_to_file(self) -> None:
        """Сохраняет текущие дефолтные настройки в config/node_defaults.json"""
        try:
            from .persistence import Persistence
            from .channels import Channels
            
            # Создаем дефолтные каналы для сохранения
            channels = Channels()
            
            defaults = {
                'channels': [],
                'config': None,
                'module_config': None,
                'owner': None
            }
            
            # Сохраняем каналы
            persistence = Persistence()
            defaults['channels'] = [persistence._channel_to_dict(ch) for ch in channels.channels]
            
            # Сохраняем config
            if self.config:
                serialized = self.config.SerializeToString()
                defaults['config'] = {
                    '_type': self.config.DESCRIPTOR.full_name,
                    '_data': base64.b64encode(serialized).decode('utf-8')
                }
            
            # Сохраняем module_config
            if self.module_config:
                serialized = self.module_config.SerializeToString()
                defaults['module_config'] = {
                    '_type': self.module_config.DESCRIPTOR.full_name,
                    '_data': base64.b64encode(serialized).decode('utf-8')
                }
            
            # Создаем директорию, если её нет
            NODE_DEFAULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            
            with open(NODE_DEFAULTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(defaults, f, indent=2, ensure_ascii=False)
        except Exception as e:
            # Игнорируем ошибки сохранения дефолтов
            pass

