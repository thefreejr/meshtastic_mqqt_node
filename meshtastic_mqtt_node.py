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
import socket
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
from meshtastic_simulator.bots import BotManager
from meshtastic_simulator.api.server import APIServer


def get_local_ip() -> str:
    """Получает локальный IP адрес для подключения из сети"""
    try:
        # Подключаемся к внешнему адресу (не отправляем данные)
        # Это нужно чтобы определить IP адрес сетевого интерфейса
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        try:
            # Не отправляем данные, просто определяем интерфейс
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
        except Exception:
            ip = '127.0.0.1'
        finally:
            s.close()
        return ip
    except Exception:
        return '127.0.0.1'


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
    parser.add_argument('--api-port', type=int, default=8080,
                       help='API порт (по умолчанию: 8080)')
    parser.add_argument('--api-host', default='0.0.0.0',
                       help='API хост (по умолчанию: 0.0.0.0)')
    parser.add_argument('--no-api', action='store_true',
                       help='Не запускать API сервер')
    parser.add_argument('--no-bots', action='store_true',
                       help='Не загружать ботов')
    
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
        print(f"📝 Logs are written to file: {DEFAULT_LOG_FILE}")
    
    # Получаем локальный IP адрес
    local_ip = get_local_ip()
    
    print("="*70)
    print("Meshtastic MQTT Node Simulator (Multi-Session)")
    print("="*70)
    print(f"MQTT Defaults: {args.mqtt_broker}:{args.mqtt_port}")
    print(f"TCP: {local_ip}:{args.tcp_port} (0.0.0.0:{args.tcp_port})")
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
    
    # Создаем менеджер ботов
    bot_manager = None
    if not args.no_bots:
        bot_manager = BotManager()
        loaded_bots = bot_manager.load_bots_from_config()
        if loaded_bots:
            print(f"\n✓ Loaded {len(loaded_bots)} bots: {', '.join(loaded_bots)}")
        else:
            print("\n✓ No bots configured")
    
    # Создаем API сервер
    api_server = None
    api_thread = None
    if not args.no_api:
        try:
            api_server = APIServer(bot_manager=bot_manager, host=args.api_host, port=args.api_port)
            api_thread = threading.Thread(target=api_server.start, daemon=True)
            api_thread.start()
            print(f"\n✓ API server started on http://{args.api_host}:{args.api_port}")
            print(f"  API docs: http://{args.api_host}:{args.api_port}/docs")
        except Exception as e:
            print(f"\n⚠ Failed to start API server: {e}")
            print("  Install dependencies: pip install fastapi uvicorn[standard]")
    
    print("\n✓ Server started")
    print(f"  Local connection: meshtastic --host localhost:{args.tcp_port}")
    if local_ip != '127.0.0.1':
        print(f"  Network connection: meshtastic --host {local_ip}:{args.tcp_port}")
    print("\n👂 Waiting for connections... (Ctrl+C to exit)\n")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n⚠ Stopping...")
        try:
            # Останавливаем ботов
            if bot_manager:
                bot_manager.stop_all()
                print("✓ Bots stopped")
            
            # Останавливаем TCP сервер
            tcp_server.stop()
            
            # Даем время на корректное завершение всех потоков
            time.sleep(0.5)
        except Exception as e:
            print(f"Error during shutdown: {e}")
        finally:
            # Закрываем файл логов если был открыт
            set_log_file(None)  # Закрывает файл
        print("✓ Stopped")
        return 0


if __name__ == '__main__':
    exit(main())
