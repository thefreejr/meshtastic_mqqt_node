# Meshtastic MQTT Node Simulator

Python симулятор Meshtastic ноды с поддержкой MQTT и TCP StreamAPI. Позволяет эмулировать работу Meshtastic устройства без физического радиомодуля, подключаясь к MQTT брокеру и принимая подключения от Meshtastic Python CLI через TCP.

## Возможности

- ✅ **TCP StreamAPI сервер** - подключение Meshtastic Python CLI через `meshtastic --host localhost:4403`
- ✅ **MQTT интеграция** - подключение к MQTT брокеру Meshtastic для обмена сообщениями
- ✅ **Мультисессионная архитектура** - поддержка нескольких одновременных подключений
- ✅ **Сохранение настроек** - настройки узлов сохраняются между перезапусками
- ✅ **PKI поддержка** - генерация Curve25519 ключей для шифрования
- ✅ **Каналы** - поддержка до 8 каналов с индивидуальными PSK ключами
- ✅ **Admin API** - полная поддержка Admin сообщений (channels, config, owner, etc.)
- ✅ **Файловое логирование** - логи записываются в `logs/simulator.log`
- ✅ **Docker поддержка** - готовые Dockerfile и docker-compose.yml
- ✅ **Централизованная конфигурация** - настройки в `config/project.yaml`

## Архитектура

```
┌──────────────────┐         ┌──────────────┐         ┌─────────────┐
│  Meshtastic CLI  │◄──────►│  TCP Server  │◄──────►│  MQTT       │
│  (Python)        │ StreamAPI│  (Port 4403)  │  MQTT  │  Broker     │
└──────────────────┘         └──────────────┘         └─────────────┘
                                     │
                                     │
                          ┌──────────▼──────────┐
                          │  Session Manager    │
                          │  - Multi-session    │
                          │  - Settings persist │
                          │  - Node ID mapping  │
                          └─────────────────────┘
```

### Структура модулей

```
meshtastic_simulator/
├── tcp/              # TCP сервер и сессии
│   ├── server.py     # Мультисессионный TCP сервер
│   └── session.py    # Обработка TCP сессий
├── mqtt/             # MQTT клиент
│   ├── connection.py # Управление подключением
│   ├── subscription.py # Управление подписками
│   └── packet_processor.py # Обработка MQTT пакетов
├── protocol/         # Обработчики протокола
│   ├── packet_handler.py # Обработка MeshPacket
│   ├── admin_handler.py # Обработка AdminMessage
│   └── config_sender.py # Отправка конфигурации
├── mesh/             # Mesh логика
│   ├── channels.py   # Управление каналами
│   ├── config_storage.py # Хранение конфигурации
│   ├── persistence.py # Сохранение настроек
│   ├── node_db.py    # База данных узлов
│   ├── pki_manager.py # Управление PKI ключами
│   └── settings_loader.py # Загрузка настроек
└── utils/            # Утилиты
    ├── logger.py     # Логирование
    └── exceptions.py # Обработка ошибок
```

## Установка

### Локальная установка

```bash
# Установка зависимостей
pip install -r requirements_meshtastic_node.txt
```

Или вручную:
```bash
pip install paho-mqtt meshtastic protobuf cryptography pyyaml
```

### Docker

```bash
# Запуск через docker-compose
docker-compose up -d

# Или сборка образа
docker build -t meshtastic-node .
docker run -p 4403:4403 -v ./config:/app/config -v ./logs:/app/logs meshtastic-node
```

## Конфигурация

### Основной файл конфигурации: `config/project.yaml`

```yaml
# MQTT настройки по умолчанию
mqtt:
  address: "mqtt.meshtastic.org"
  port: 1883
  username: "meshdev"
  password: "large4cats"
  root: "msh"

# TCP сервер
tcp:
  host: "0.0.0.0"
  port: 4403

# Логирование
logging:
  level: "DEBUG"  # DEBUG, INFO, WARN, ERROR, NONE
  categories: null  # null = все категории, или список: ["TCP", "MQTT", "ADMIN"]
  file: "logs/simulator.log"  # null = не записывать в файл

# Настройки узла
node:
  user_long_name: "MQTT Node"
  user_short_name: "MQTT"
  hw_model: "PORTDUINO"
  firmware_version: "2.6.11.60ec05e"
```

### Настройки узлов по умолчанию: `config/node_defaults.json`

Содержит дефолтные настройки для всех узлов:
- Каналы (channels)
- Конфигурация (config)
- Конфигурация модулей (module_config)
- Владелец (owner)

### Индивидуальные настройки узлов: `config/nodes/node_!NODEID.json`

Настройки для конкретного узла сохраняются автоматически при изменении через Admin API.

## Использование

### Базовое использование

```bash
python meshtastic_mqtt_node.py
```

Скрипт запустит TCP сервер на порту 4403 (по умолчанию) и подключится к MQTT брокеру из `config/project.yaml`.

### Подключение Meshtastic CLI

```bash
# Подключение к симулятору
meshtastic --host localhost:4403

# После подключения можно использовать все команды Meshtastic CLI
meshtastic --host localhost:4403 --info
meshtastic --host localhost:4403 --sendtext "Hello World"
meshtastic --host localhost:4403 --ch-set 1 --name "MyChannel" --psk "AQ=="
```

### Параметры командной строки

```bash
python meshtastic_mqtt_node.py [OPTIONS]
```

**MQTT параметры:**
- `--mqtt-broker` - адрес MQTT брокера (по умолчанию: из config/project.yaml)
- `--mqtt-port` - порт MQTT (по умолчанию: 1883)
- `--mqtt-username` - MQTT username (по умолчанию: из config/project.yaml)
- `--mqtt-password` - MQTT password (по умолчанию: из config/project.yaml)
- `--mqtt-root` - корневой MQTT топик (по умолчанию: `msh`)

**TCP параметры:**
- `--tcp-port` - порт TCP сервера (по умолчанию: 4403 из config/project.yaml)

**Параметры логирования:**
- `--log-level LEVEL` - уровень логирования (DEBUG, INFO, WARN, ERROR, NONE)
- `--log-categories CATEGORIES` - фильтр категорий (через запятую: TCP,MQTT,ADMIN)

**Устаревшие параметры:**
- `--node-id` - ⚠️ **Deprecated**: Каждая сессия теперь автоматически генерирует свой node_id

**Примеры:**

```bash
# Только ошибки
python meshtastic_mqtt_node.py --log-level ERROR

# Только TCP и MQTT логи
python meshtastic_mqtt_node.py --log-categories TCP,MQTT

# С кастомным MQTT брокером
python meshtastic_mqtt_node.py --mqtt-broker mqtt.example.com --mqtt-port 1883

# С кастомным TCP портом
python meshtastic_mqtt_node.py --tcp-port 4404
```

**Примечание:** Файл логов настраивается в `config/project.yaml` (параметр `logging.file`), а не через командную строку.

## Структура данных

### Сохранение настроек

Настройки сохраняются в следующих файлах:

- `config/node_defaults.json` - дефолтные настройки для всех узлов
- `config/nodes/node_!NODEID.json` - индивидуальные настройки узла
- `config/device_id_mapping.json` - маппинг device_id → node_id
- `config/ip_to_node_id_mapping.json` - маппинг IP → node_id (для временной идентификации)

### Каналы

Каналы настраиваются через Admin API или вручную в `config/node_defaults.json`:

```json
{
  "channels": [
    {
      "index": 0,
      "role": 1,
      "name": "LongFast",
      "psk": "1PG7OiApB1nwvP+rz05pAQ==",
      "uplink_enabled": true,
      "downlink_enabled": true
    }
  ]
}
```

**PSK форматы:**
- Полный ключ (16 байт): base64-encoded (например, `"1PG7OiApB1nwvP+rz05pAQ=="`)
- PSK alias (1 байт): base64-encoded один байт (например, `"AQ=="` для alias=1)
- Пустой PSK: `""` (для SECONDARY каналов используется PRIMARY ключ)

## MQTT топики

Сообщения публикуются и подписываются на топики:

```
{root_topic}/2/e/{channel_id}/{gateway_id}
```

Где:
- `{root_topic}` - корневой топик (по умолчанию: `msh`)
- `2/e` - версия протокола и тип (encrypted)
- `{channel_id}` - ID канала (например, `LongFast`, `Custom`, `PKI`)
- `{gateway_id}` - Node ID шлюза (например, `!51A24D8E`)

**Примеры топиков:**
- `msh/2/e/LongFast/!51A24D8E` - PRIMARY канал
- `msh/2/e/Custom/!51A24D8E` - Custom канал
- `msh/2/e/PKI/!51A24D8E` - PKI канал

## Логирование

Логи записываются в:
- **Консоль** - всегда (stdout)
- **Файл** - если указан в `config/project.yaml` (по умолчанию: `logs/simulator.log`)

**Категории логов:**
- `TCP` - TCP сервер и сессии
- `MQTT` - MQTT подключение и сообщения
- `ADMIN` - Admin API запросы
- `CONFIG` - Отправка конфигурации
- `PERSISTENCE` - Сохранение/загрузка настроек
- `PKI` - PKI ключи
- `NODE` - NodeDB операции
- `ACK` - ACK пакеты

**Уровни логирования:**
- `DEBUG` - детальная отладочная информация
- `INFO` - информационные сообщения
- `WARN` - предупреждения
- `ERROR` - ошибки
- `NONE` - отключить логирование

## Docker

### Быстрый старт

```bash
# Запуск через docker-compose
docker-compose up -d

# Просмотр логов
docker-compose logs -f

# Остановка
docker-compose down
```

### Volumes

Docker Compose монтирует следующие директории:
- `./config` → `/app/config` - конфигурация и настройки узлов
- `./logs` → `/app/logs` - логи симулятора
- `./data` → `/app/data` - дополнительные данные (если нужно)

### Переменные окружения

Можно переопределить настройки MQTT через переменные окружения в `docker-compose.yml`:

```yaml
environment:
  - MQTT_BROKER=mqtt.example.com
  - MQTT_PORT=1883
```

## Примеры использования

### Пример 1: Базовое подключение

```bash
# 1. Запуск симулятора
python meshtastic_mqtt_node.py

# 2. В другом терминале - подключение CLI
meshtastic --host localhost:4403 --info
```

### Пример 2: Настройка канала

```bash
# Подключение и настройка канала 1
meshtastic --host localhost:4403 \
  --ch-set 1 \
  --name "MyChannel" \
  --psk "AQ==" \
  --uplink \
  --downlink
```

### Пример 3: Отправка сообщения

```bash
# Отправка текстового сообщения
meshtastic --host localhost:4403 --sendtext "Hello from simulator!"
```

### Пример 4: Просмотр настроек

```bash
# Просмотр информации об узле
meshtastic --host localhost:4403 --info

# Просмотр каналов
meshtastic --host localhost:4403 --ch-get 1
```

## Troubleshooting

### Не могу подключиться к TCP серверу

- Проверьте, что сервер запущен: `netstat -an | grep 4403`
- Проверьте firewall настройки
- Убедитесь, что порт 4403 не занят другим процессом

### MQTT не подключается

- Проверьте настройки в `config/project.yaml`
- Проверьте доступность MQTT брокера
- Проверьте логи: `tail -f logs/simulator.log | grep MQTT`
- Убедитесь, что username/password правильные

### Настройки не сохраняются

- Проверьте права на запись в `config/nodes/`
- Проверьте логи на ошибки сохранения
- Убедитесь, что `node_id` установлен правильно

### Каналы не работают

- Проверьте, что канал имеет правильный PSK
- Убедитесь, что `uplink_enabled` или `downlink_enabled` установлены
- Проверьте hash канала: должен совпадать на всех узлах

### Ошибки расшифровки пакетов

- Убедитесь, что PSK ключи совпадают на всех узлах
- Проверьте, что канал правильно настроен
- Для PKI: убедитесь, что публичные ключи обменяны

## Ограничения

1. **Без радиомодулей**: Симулятор не имеет физических радиомодулей, только MQTT мост
2. **Упрощенная обработка**: Некоторые функции реальной ноды упрощены
3. **PKI расшифровка**: Полная PKI расшифровка требует дополнительной реализации (PKI ключи генерируются, но расшифровка пока не реализована)
4. **Один MQTT брокер**: Каждая сессия подключается к одному MQTT брокеру (настройки из config/project.yaml или командной строки)

## Технические детали

### PSK (Pre-Shared Key) логика

PSK ключи обрабатываются согласно firmware:
- **PSK alias (1 байт)**: Расширяется до полного `defaultpsk` с модификацией последнего байта
- **Короткий PSK (< 16 байт)**: Дополняется нулями до 16 байт (AES128)
- **Средний PSK (16-32 байта)**: Дополняется нулями до 32 байт (AES256)
- **Полный PSK (16 или 32 байта)**: Используется как есть
- **SECONDARY каналы с пустым PSK**: Используют PRIMARY ключ

### Сохранение настроек

Настройки автоматически сохраняются при изменении через Admin API:
- Каналы → `config/nodes/node_!NODEID.json`
- Конфигурация → `config/nodes/node_!NODEID.json`
- Module Config → `config/nodes/node_!NODEID.json`
- Owner → `config/nodes/node_!NODEID.json`

Маппинги для сохранения между перезапусками:
- `device_id → node_id` и `IP → node_id` → `config/node_id_mapping.json` (объединенный файл)

## Разработка

### Структура проекта

```
.
├── meshtastic_mqtt_node.py    # Главный скрипт
├── meshtastic_simulator/      # Основной пакет
│   ├── tcp/                   # TCP сервер и сессии
│   │   ├── server.py          # Мультисессионный TCP сервер
│   │   └── session.py         # Обработка TCP сессий
│   ├── mqtt/                  # MQTT клиент
│   │   ├── connection.py      # Управление подключением
│   │   ├── subscription.py   # Управление подписками
│   │   ├── packet_processor.py # Обработка MQTT пакетов
│   │   └── client.py         # Основной MQTT клиент (legacy)
│   ├── protocol/              # Обработчики протокола
│   │   ├── packet_handler.py # Обработка MeshPacket
│   │   ├── admin_handler.py  # Обработка AdminMessage
│   │   ├── config_sender.py  # Отправка конфигурации
│   │   └── stream_api.py     # StreamAPI framing
│   ├── mesh/                  # Mesh логика
│   │   ├── channels.py        # Управление каналами
│   │   ├── config_storage.py  # Хранение конфигурации
│   │   ├── persistence.py    # Сохранение настроек
│   │   ├── node_db.py         # База данных узлов
│   │   ├── pki_manager.py     # Управление PKI ключами
│   │   ├── settings_loader.py # Загрузка настроек
│   │   ├── rtc.py             # Работа со временем
│   │   └── crypto.py          # Криптография (legacy)
│   ├── utils/                 # Утилиты
│   │   ├── logger.py          # Логирование
│   │   └── exceptions.py       # Обработка ошибок
│   ├── config_loader/          # Загрузчик конфигурации
│   │   └── loader.py          # ConfigLoader (singleton)
│   └── config.py              # Константы и настройки
├── config/                     # Конфигурация
│   ├── project.yaml           # Основная конфигурация
│   ├── node_defaults.json     # Дефолтные настройки
│   ├── node_id_mapping.json # Объединенный маппинг (device_id и IP → node_id)
│   └── nodes/                 # Индивидуальные настройки
│       └── node_!NODEID.json # Настройки конкретного узла
├── logs/                      # Логи
│   └── simulator.log          # Файл логов
├── Dockerfile                 # Docker образ
├── docker-compose.yml         # Docker Compose
└── requirements_meshtastic_node.txt # Зависимости
```

### Запуск тестов

```bash
# Проверка импортов
python -c "from meshtastic_simulator import *; print('OK')"

# Проверка конфигурации
python -c "from meshtastic_simulator.config_loader import ConfigLoader; c = ConfigLoader.get_instance(); print(c.get_value('mqtt.broker'))"
```

## Лицензия

Скрипт предоставляется как есть, для использования с проектом Meshtastic.

## См. также

- [README_DOCKER.md](README_DOCKER.md) - подробная документация по Docker
- [REFACTORING_STATUS.md](REFACTORING_STATUS.md) - статус рефакторинга
- [Meshtastic Python CLI](https://github.com/meshtastic/python) - официальный Meshtastic CLI
