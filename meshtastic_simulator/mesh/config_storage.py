"""
Хранилище конфигурации узла (из firmware/src/mesh/NodeDB.cpp)
"""

from meshtastic import mesh_pb2
from meshtastic.protobuf import config_pb2, module_config_pb2, admin_pb2
from typing import Optional

# Избегаем циклического импорта - импортируем только при необходимости
# from ..config import NodeConfig


class ConfigStorage:
    """Хранилище конфигурации узла (аналог config в firmware)"""
    
    def __init__(self):
        # Инициализируем конфигурацию по умолчанию (как в firmware NodeDB::installDefaultConfig)
        self.config = config_pb2.Config()
        self._init_default_config()
        
        self.module_config = module_config_pb2.ModuleConfig()
        self._init_default_module_config()
    
    def _init_default_config(self):
        """Инициализирует конфигурацию по умолчанию (как в firmware NodeDB::installDefaultConfig)"""
        # Device Config
        self.config.device.role = config_pb2.Config.DeviceConfig.Role.CLIENT
        self.config.device.rebroadcast_mode = config_pb2.Config.DeviceConfig.RebroadcastMode.ALL
        
        # LoRa Config
        self.config.lora.tx_enabled = True
        self.config.lora.sx126x_rx_boosted_gain = True
        self.config.lora.override_duty_cycle = False
        self.config.lora.config_ok_to_mqtt = False
        # По умолчанию используем LongFast preset (из firmware)
        self.config.lora.modem_preset = config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST
        self.config.lora.region = config_pb2.Config.LoRaConfig.RegionCode.UNSET
        
        # Position Config
        # По умолчанию используем значения из firmware Default.h
        self.config.position.gps_update_interval = 2 * 60  # 2 минуты для клиента
        self.config.position.position_broadcast_secs = 15 * 60  # 15 минут для клиента
        self.config.position.gps_mode = config_pb2.Config.PositionConfig.GpsMode.ENABLED
        
        # Power Config
        self.config.power.ls_secs = 5 * 60  # 5 минут для клиента
        self.config.power.min_wake_secs = 10
        self.config.power.sds_secs = 0xFFFFFFFF  # Forever для клиента
        self.config.power.wait_bluetooth_secs = 60  # 60 секунд для клиента
        self.config.power.is_power_saving = False
        
        # Display Config
        self.config.display.screen_on_secs = 60 * 10  # 10 минут для клиента
        self.config.display.displaymode = config_pb2.Config.DisplayConfig.DisplayMode.DEFAULT
        
        # Network Config (по умолчанию пустой, но можно настроить)
        self.config.network.wifi_enabled = False
        self.config.network.eth_enabled = False  # eth_enabled, а не ethernet_enabled
        
        # Bluetooth Config (по умолчанию пустой)
        self.config.bluetooth.enabled = False
        
        # Security Config (по умолчанию пустой)
        # Не устанавливаем ключи по умолчанию для безопасности
    
    def _init_default_module_config(self):
        """Инициализирует конфигурацию модулей по умолчанию (как в firmware NodeDB::installDefaultModuleConfig)"""
        # MQTT Config (по умолчанию из Default.h)
        self.module_config.mqtt.enabled = True
        self.module_config.mqtt.address = "mqtt.meshtastic.org"
        self.module_config.mqtt.username = "meshdev"
        self.module_config.mqtt.password = "large4cats"
        self.module_config.mqtt.root = "msh"
        self.module_config.mqtt.encryption_enabled = True
        self.module_config.mqtt.tls_enabled = False
        
        # Serial Config
        self.module_config.serial.enabled = False
        self.module_config.serial.timeout = 5000
        self.module_config.serial.mode = 0  # 0 = DEFAULT (mode это int, а не enum)
        
        # External Notification Config
        self.module_config.external_notification.enabled = False  # По умолчанию выключено для симулятора
        
        # Store & Forward Config
        self.module_config.store_forward.enabled = False
        
        # Range Test Config
        self.module_config.range_test.enabled = False
        
        # Telemetry Config
        self.module_config.telemetry.device_update_interval = 30 * 60  # 30 минут (min_default_telemetry_interval_secs)
        self.module_config.telemetry.environment_update_interval = 60 * 60  # 1 час
        self.module_config.telemetry.environment_measurement_enabled = False
        
        # Canned Message Config
        self.module_config.canned_message.enabled = False
        # messages хранятся отдельно, не в CannedMessageConfig
        # Будем хранить их отдельно в TCPServer
        
        # Audio Config
        self.module_config.audio.codec2_enabled = False
        self.module_config.audio.ptt_pin = 0
        self.module_config.audio.bitrate = 3200
        
        # Remote Hardware Config
        self.module_config.remote_hardware.enabled = False
        
        # Neighbor Info Config
        self.module_config.neighbor_info.enabled = False
        self.module_config.neighbor_info.update_interval = 6 * 60 * 60  # 6 часов
        
        # Ambient Lighting Config
        # AmbientLightingConfig не имеет поля enabled, только led_state, red, green, blue, current
        
        # Detection Sensor Config
        self.module_config.detection_sensor.enabled = False
        
        # Paxcounter Config
        self.module_config.paxcounter.enabled = False
    
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
            response.session_key.CopyFrom(self.config.session_key)
        elif config_type == admin_pb2.AdminMessage.ConfigType.DEVICEUI_CONFIG:
            # DeviceUI config - пустой (как в firmware)
            response.device_ui.CopyFrom(self.config.device_ui)
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
    
    def set_config(self, config: config_pb2.Config):
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
    
    def set_module_config(self, module_config: module_config_pb2.ModuleConfig):
        """Устанавливает конфигурацию модуля (как в firmware AdminModule::handleSetModuleConfig)"""
        # Обновляем соответствующие поля в self.module_config
        if module_config.HasField('mqtt'):
            self.module_config.mqtt.CopyFrom(module_config.mqtt)
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

