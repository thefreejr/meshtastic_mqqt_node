#!/usr/bin/env python3
"""
Meshtastic MQTT Node Simulator

Имитирует работу ноды Meshtastic с MQTT и TCP сервером (StreamAPI).
Основано на структуре firmware/src/mqtt/MQTT.cpp и firmware/src/mesh/StreamAPI.cpp

Использование:
    python meshtastic_mqtt_node.py --mqtt-broker mqtt.meshtastic.org --mqtt-port 1883
    meshtastic --host localhost:4403
"""

import argparse
import threading
import time

# Импорты из новой модульной структуры
from meshtastic_simulator.config import (
    DEFAULT_MQTT_ADDRESS, DEFAULT_MQTT_USERNAME, DEFAULT_MQTT_PASSWORD, 
    DEFAULT_MQTT_ROOT, MAX_NUM_CHANNELS, DEFAULT_LOG_LEVEL, DEFAULT_LOG_CATEGORIES
)
from meshtastic_simulator.utils.logger import set_log_level, set_log_categories, LogLevel
from meshtastic_simulator.mesh import Channels, NodeDB, generate_node_id
from meshtastic_simulator.mqtt import MQTTClient
from meshtastic_simulator.tcp import TCPServer
from meshtastic.protobuf import channel_pb2


def main():
    """Главная функция для запуска симулятора"""
    parser = argparse.ArgumentParser(
        description='Meshtastic MQTT Node Simulator',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python meshtastic_mqtt_node.py
  python meshtastic_mqtt_node.py --mqtt-broker mqtt.meshtastic.org --mqtt-port 1883
  meshtastic --host localhost:4403
        """
    )
    
    parser.add_argument('--mqtt-broker', default=DEFAULT_MQTT_ADDRESS,
                       help=f'MQTT брокер (по умолчанию: {DEFAULT_MQTT_ADDRESS})')
    parser.add_argument('--mqtt-port', type=int, default=1883,
                       help='MQTT порт (по умолчанию: 1883)')
    parser.add_argument('--mqtt-username', default=DEFAULT_MQTT_USERNAME,
                       help=f'MQTT username (по умолчанию: {DEFAULT_MQTT_USERNAME})')
    parser.add_argument('--mqtt-password', default=DEFAULT_MQTT_PASSWORD,
                       help=f'MQTT password (по умолчанию: {DEFAULT_MQTT_PASSWORD})')
    parser.add_argument('--mqtt-root', default=DEFAULT_MQTT_ROOT,
                       help=f'MQTT корневой топик (по умолчанию: {DEFAULT_MQTT_ROOT})')
    parser.add_argument('--node-id', default=None,
                       help='Node ID (по умолчанию: автогенерация)')
    parser.add_argument('--tcp-port', type=int, default=4403,
                       help='TCP порт (по умолчанию: 4403)')
    parser.add_argument('--log-level', choices=['DEBUG', 'INFO', 'WARN', 'ERROR', 'NONE'],
                       default=None,
                       help='Уровень логирования (по умолчанию: из config.py)')
    parser.add_argument('--log-categories', type=str, default=None,
                       help='Категории логов (через запятую, например: TCP,MQTT,ADMIN). По умолчанию: все категории')
    
    args = parser.parse_args()
    
    # Устанавливаем уровень логирования
    if args.log_level:
        log_level = LogLevel[args.log_level]
    else:
        log_level = DEFAULT_LOG_LEVEL
    set_log_level(log_level)
    
    # Устанавливаем фильтр категорий
    if args.log_categories:
        # Парсим список категорий из строки (например: "TCP,MQTT,ADMIN")
        categories = [cat.strip().upper() for cat in args.log_categories.split(',') if cat.strip()]
        set_log_categories(categories)
    else:
        # Используем настройку из config.py
        set_log_categories(DEFAULT_LOG_CATEGORIES)
    
    # Генерируем Node ID если не указан
    node_id = args.node_id or generate_node_id()
    
    print("="*70)
    print("Meshtastic MQTT Node Simulator")
    print("="*70)
    print(f"Node ID: {node_id}")
    print(f"MQTT: {args.mqtt_broker}:{args.mqtt_port}")
    print(f"TCP: localhost:{args.tcp_port}")
    print()
    
    # Получаем node_num из node_id
    try:
        node_num = int(node_id[1:], 16) if node_id.startswith('!') else int(node_id, 16)
    except:
        node_num = 0x12345678
    
    # Создаем каналы
    channels = Channels()
    
    # Логируем hash каналов для проверки
    for i in range(MAX_NUM_CHANNELS):
        ch = channels.get_by_index(i)
        if ch.role != channel_pb2.Channel.Role.DISABLED:
            ch_hash = channels.get_hash(i)
            ch_name = channels.get_global_id(i)
            print(f"📊 Канал {i} ({ch_name}): hash={ch_hash} (0x{ch_hash:02x})")
    
    # Создаем NodeDB
    node_db = NodeDB(our_node_num=node_num)
    
    # Создаем MQTT клиент
    mqtt_client = MQTTClient(
        broker=args.mqtt_broker,
        port=args.mqtt_port,
        username=args.mqtt_username,
        password=args.mqtt_password,
        root_topic=args.mqtt_root,
        node_id=node_id,
        channels=channels,
        node_db=node_db
    )
    
    if not mqtt_client.start():
        print("✗ Не удалось подключиться к MQTT")
        return 1
    
    # Создаем TCP сервер
    tcp_server = TCPServer(port=args.tcp_port, mqtt_client=mqtt_client, channels=channels, node_db=node_db)
    tcp_thread = threading.Thread(target=tcp_server.start, daemon=True)
    tcp_thread.start()
    
    print("\n✓ Сервер запущен")
    print(f"  Подключение: meshtastic --host localhost:{args.tcp_port}")
    print("\n👂 Ожидание подключений... (Ctrl+C для выхода)\n")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n⚠ Остановка...")
        tcp_server.stop()
        mqtt_client.stop()
        print("✓ Остановлено")
        return 0


if __name__ == '__main__':
    exit(main())
