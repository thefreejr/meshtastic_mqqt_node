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
    DEFAULT_MQTT_ROOT, DEFAULT_LOG_LEVEL, DEFAULT_LOG_CATEGORIES,
    DEFAULT_LOG_FILE, TCP_HOST, TCP_PORT
)
from meshtastic_simulator.utils.logger import set_log_level, set_log_categories, set_log_file, LogLevel
from meshtastic_simulator.tcp import TCPServer


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
                       help='Node ID (deprecated: each session now generates its own node_id)')
    parser.add_argument('--tcp-port', type=int, default=TCP_PORT,
                       help=f'TCP порт (по умолчанию: {TCP_PORT} из config/project.yaml)')
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
    
    # Устанавливаем файл для логирования
    if DEFAULT_LOG_FILE:
        set_log_file(DEFAULT_LOG_FILE)
        print(f"📝 Логи записываются в файл: {DEFAULT_LOG_FILE}")
    
    print("="*70)
    print("Meshtastic MQTT Node Simulator (Multi-Session)")
    print("="*70)
    print(f"MQTT Defaults: {args.mqtt_broker}:{args.mqtt_port}")
    print(f"TCP: localhost:{args.tcp_port}")
    print("  (Each client will get its own node_id and settings)")
    print()
    
    # Создаем TCP сервер (мультисессионная архитектура)
    # Каждая сессия создает свои компоненты (channels, node_db, mqtt_client)
    tcp_server = TCPServer(
        port=args.tcp_port,
        default_mqtt_broker=args.mqtt_broker,
        default_mqtt_port=args.mqtt_port,
        default_mqtt_username=args.mqtt_username,
        default_mqtt_password=args.mqtt_password,
        default_mqtt_root=args.mqtt_root
    )
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
        try:
            tcp_server.stop()
            # Даем время на корректное завершение всех потоков
            time.sleep(0.5)
        except Exception as e:
            print(f"Ошибка при остановке: {e}")
        finally:
            # Закрываем файл логов если был открыт
            set_log_file(None)  # Закрывает файл
        print("✓ Остановлено")
        return 0


if __name__ == '__main__':
    exit(main())
