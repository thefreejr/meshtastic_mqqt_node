"""
Отправка конфигурации клиенту
"""

import time
from typing import Callable, Optional, Any

try:
    from meshtastic import mesh_pb2
    try:
        from meshtastic.protobuf import telemetry_pb2
    except ImportError:
        telemetry_pb2 = None
except ImportError:
    print("Ошибка: Установите meshtastic: pip install meshtastic")
    raise

from ..config import MAX_NUM_CHANNELS
from ..mesh.config_storage import NodeConfig
from ..mesh.rtc import RTCQuality, get_valid_time
from ..utils.logger import info, debug, warn, error

try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False


class ConfigSender:
    """Отправка конфигурации клиенту"""
    
    def __init__(self, 
                 node_id: str,
                 node_num: int,
                 owner: Any,  # mesh_pb2.User
                 channels: Any,  # Channels
                 config_storage: Any,  # ConfigStorage
                 node_db: Any,  # NodeDB
                 pki_public_key: Optional[bytes],
                 rtc: Any,  # RTC
                 send_from_radio_callback: Callable[[mesh_pb2.FromRadio], None],
                 device_id: Optional[bytes] = None) -> None:
        """
        Инициализирует ConfigSender
        
        Args:
            node_id: Node ID (строка вида !XXXXXXXX)
            node_num: Node number (int)
            owner: Объект User (владелец)
            channels: Объект Channels
            config_storage: Объект ConfigStorage
            node_db: Объект NodeDB
            pki_public_key: Публичный ключ PKI (32 байта) или None
            rtc: Объект RTC
            send_from_radio_callback: Функция для отправки FromRadio (принимает mesh_pb2.FromRadio)
            device_id: Уже установленный device_id (опционально)
        """
        self.node_id = node_id
        self.node_num = node_num
        self.owner = owner
        self.channels = channels
        self.config_storage = config_storage
        self.node_db = node_db
        self.pki_public_key = pki_public_key
        self.rtc = rtc
        self.send_from_radio = send_from_radio_callback
        self._device_id = device_id
    
    def send_config(self, config_nonce: int) -> None:
        """
        Отправляет конфигурацию клиенту
        
        Args:
            config_nonce: Nonce для config_complete_id
        """
        # Логирование без префикса, так как префикс добавляется в session.py
        info("CONFIG", f"Отправка конфигурации (nonce: {config_nonce})")
        
        # 1. MyInfo
        self._send_my_info()
        
        # 2. NodeInfo
        self._send_node_info()
        
        # 3. Metadata
        self._send_metadata()
        
        # 4. Channels
        self._send_channels()
        
        # 5. Other NodeInfos
        self._send_other_nodes()
        
        # 6. Config complete
        self._send_config_complete(config_nonce)
    
    def _send_my_info(self) -> None:
        """Отправляет MyInfo"""
        my_info = mesh_pb2.MyNodeInfo()
        my_info.my_node_num = self.node_num & 0x7FFFFFFF
        
        # Устанавливаем device_id (если еще не установлен)
        if not self._device_id:
            device_id_bytes = bytearray(16)
            try:
                mac_hex = self.node_id[1:] if self.node_id.startswith('!') else self.node_id
                device_id_bytes[0] = 0x80
                device_id_bytes[1] = 0x38
                for i in range(0, min(6, len(mac_hex) // 2)):
                    if i + 2 < len(mac_hex):
                        device_id_bytes[i + 2] = int(mac_hex[i*2:(i+1)*2], 16)
            except:
                device_id_bytes[0:4] = self.node_num.to_bytes(4, 'little')
                device_id_bytes[4:8] = (self.node_num >> 32).to_bytes(4, 'little') if self.node_num > 0xFFFFFFFF else b'\x00\x00\x00\x00'
            
            self._device_id = bytes(device_id_bytes)
            debug("CONFIG", f"Device ID установлен: {self._device_id.hex()}")
        
        my_info.device_id = self._device_id
        
        from_radio = mesh_pb2.FromRadio()
        from_radio.my_info.CopyFrom(my_info)
        self.send_from_radio(from_radio)
    
    def _send_node_info(self) -> None:
        """Отправляет NodeInfo"""
        node_info = mesh_pb2.NodeInfo()
        node_info.num = self.node_num & 0x7FFFFFFF
        node_info.user.id = self.owner.id
        node_info.user.long_name = self.owner.long_name
        node_info.user.short_name = self.owner.short_name
        node_info.user.is_licensed = self.owner.is_licensed
        
        if self.owner.public_key and len(self.owner.public_key) > 0:
            if not self.owner.is_licensed:
                node_info.user.public_key = self.owner.public_key
        
        try:
            if NodeConfig.HW_MODEL == "PORTDUINO":
                node_info.user.hw_model = mesh_pb2.HardwareModel.PORTDUINO
            else:
                hw_model_attr = getattr(mesh_pb2.HardwareModel, NodeConfig.HW_MODEL, None)
                if hw_model_attr is not None:
                    node_info.user.hw_model = hw_model_attr
                else:
                    node_info.user.hw_model = mesh_pb2.HardwareModel.PORTDUINO
        except Exception as e:
            debug("CONFIG", f"Не удалось установить hw_model: {e}")
        
        if self.pki_public_key and len(self.pki_public_key) == 32:
            node_info.user.public_key = self.pki_public_key
            info("PKI", f"Публичный ключ добавлен в NodeInfo ({self.pki_public_key[:8].hex()}...)")
        
        if telemetry_pb2:
            try:
                our_node = self.node_db.get_mesh_node(self.node_num)
                if our_node and hasattr(our_node, 'device_metrics'):
                    node_info.device_metrics.CopyFrom(our_node.device_metrics)
                else:
                    device_metrics = telemetry_pb2.DeviceMetrics()
                    device_metrics.battery_level = NodeConfig.DEVICE_METRICS_BATTERY_LEVEL
                    device_metrics.voltage = NodeConfig.DEVICE_METRICS_VOLTAGE
                    device_metrics.channel_utilization = NodeConfig.DEVICE_METRICS_CHANNEL_UTILIZATION
                    device_metrics.air_util_tx = NodeConfig.DEVICE_METRICS_AIR_UTIL_TX
                    current_time = self.rtc.get_time()
                    if current_time > 0:
                        device_metrics.uptime_seconds = current_time % (365 * 24 * 3600)
                    else:
                        device_metrics.uptime_seconds = int(time.time()) % (365 * 24 * 3600)
                    node_info.device_metrics.CopyFrom(device_metrics)
                    
                    if our_node:
                        our_node.device_metrics.CopyFrom(device_metrics)
                    else:
                        our_node = self.node_db.get_or_create_mesh_node(self.node_num)
                        our_node.device_metrics.CopyFrom(device_metrics)
            except Exception as e:
                error("CONFIG", f"Ошибка добавления device_metrics: {e}")
                import traceback
                traceback.print_exc()
        
        from_radio = mesh_pb2.FromRadio()
        from_radio.node_info.CopyFrom(node_info)
        self.send_from_radio(from_radio)
    
    def _send_metadata(self) -> None:
        """Отправляет DeviceMetadata"""
        metadata = mesh_pb2.DeviceMetadata()
        metadata.firmware_version = NodeConfig.FIRMWARE_VERSION
        metadata.device_state_version = 1
        metadata.canShutdown = True
        metadata.hasWifi = False
        metadata.hasBluetooth = False
        metadata.hasEthernet = True
        metadata.role = self.config_storage.config.device.role
        metadata.position_flags = self.config_storage.config.position.position_flags
        
        try:
            if NodeConfig.HW_MODEL == "PORTDUINO":
                metadata.hw_model = mesh_pb2.HardwareModel.PORTDUINO
            else:
                hw_model_attr = getattr(mesh_pb2.HardwareModel, NodeConfig.HW_MODEL, None)
                if hw_model_attr is not None:
                    metadata.hw_model = hw_model_attr
        except Exception as e:
            debug("CONFIG", f"Не удалось установить hw_model в DeviceMetadata: {e}")
        
        metadata.hasRemoteHardware = self.config_storage.module_config.remote_hardware.enabled
        metadata.hasPKC = CRYPTOGRAPHY_AVAILABLE
        metadata.excluded_modules = mesh_pb2.ExcludedModules.EXCLUDED_NONE
        
        from_radio = mesh_pb2.FromRadio()
        from_radio.metadata.CopyFrom(metadata)
        self.send_from_radio(from_radio)
    
    def _send_channels(self) -> None:
        """Отправляет все каналы"""
        for i in range(MAX_NUM_CHANNELS):
            channel = self.channels.get_by_index(i)
            from_radio = mesh_pb2.FromRadio()
            from_radio.channel.CopyFrom(channel)
            self.send_from_radio(from_radio)
    
    def _send_other_nodes(self) -> None:
        """Отправляет информацию о других узлах"""
        all_nodes = self.node_db.get_all_nodes()
        if all_nodes:
            info("CONFIG", f"Отправка информации о {len(all_nodes)} узлах")
            for node_info in all_nodes:
                from_radio = mesh_pb2.FromRadio()
                from_radio.node_info.CopyFrom(node_info)
                self.send_from_radio(from_radio)
    
    def _send_config_complete(self, config_nonce: int) -> None:
        """Отправляет config_complete_id"""
        from_radio = mesh_pb2.FromRadio()
        from_radio.config_complete_id = config_nonce
        self.send_from_radio(from_radio)

