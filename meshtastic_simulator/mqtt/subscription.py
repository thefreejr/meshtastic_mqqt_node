"""
Управление подписками на MQTT топики
"""

from typing import Optional, Any

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Ошибка: Установите paho-mqtt: pip install paho-mqtt")
    raise

from ..config import MAX_NUM_CHANNELS
from ..mesh.channels import Channels
from ..utils.logger import info, error


class MQTTSubscription:
    """Управление подписками на MQTT топики"""
    
    def __init__(self, root_topic: str, channels: Channels, node_id: str) -> None:
        """
        Инициализирует менеджер подписок
        
        Args:
            root_topic: Корневой топик (например, "msh")
            channels: Объект Channels для получения информации о каналах
            node_id: Node ID для логирования
        """
        self.root_topic = root_topic
        self.channels = channels
        self.node_id = node_id
        self.crypt_topic = f"{root_topic}/2/e/"
    
    def subscribe_to_channels(self, client: Any) -> bool:  # mqtt.Client
        """
        Подписывается на MQTT топики каналов
        
        Args:
            client: paho.mqtt.client объект
            
        Returns:
            True если хотя бы одна подписка успешна, False иначе
        """
        has_downlink = False
        
        info("MQTT", f"🔍 Начало подписки на каналы: проверяем {MAX_NUM_CHANNELS} каналов")
        
        for i in range(MAX_NUM_CHANNELS):
            ch = self.channels.get_by_index(i)
            channel_id = self.channels.get_global_id(i)
            downlink_enabled = ch.settings.downlink_enabled
            
            # Логируем статус каждого канала
            if channel_id == "Custom":
                info("MQTT", f"🔍 Канал {i} (Custom): downlink_enabled={downlink_enabled}")
            
            if downlink_enabled:
                has_downlink = True
                topic = f"{self.crypt_topic}{channel_id}/+"
                result, mid = client.subscribe(topic, qos=1)
                if result == 0:
                    if channel_id == "Custom":
                        info("MQTT", f"✅ ПОДПИСКА НА CUSTOM: topic={topic} (канал {i}: {channel_id})")
                    else:
                        info("MQTT", f"Подписан на топик: {topic} (канал {i}: {channel_id})")
                else:
                    if channel_id == "Custom":
                        error("MQTT", f"❌ ОШИБКА ПОДПИСКИ НА CUSTOM: topic={topic} (код: {result})")
                    else:
                        error("MQTT", f"Ошибка подписки на топик: {topic} (код: {result})")
        
        # Подписываемся на PKI канал если есть хотя бы один канал с downlink
        if has_downlink:
            topic = f"{self.crypt_topic}PKI/+"
            result, mid = client.subscribe(topic, qos=1)
            if result == 0:
                info("MQTT", f"Подписан на топик: {topic}")
            else:
                error("MQTT", f"Ошибка подписки на топик: {topic} (код: {result})")
        
        return has_downlink
    
    def update_subscriptions(self, client: Any) -> bool:  # mqtt.Client
        """
        Обновляет подписки при изменении каналов
        
        Args:
            client: paho.mqtt.client объект
            
        Returns:
            True если обновление успешно, False иначе
        """
        # Для простоты просто переподписываемся на все каналы
        # В будущем можно оптимизировать, отслеживая изменения
        return self.subscribe_to_channels(client)

