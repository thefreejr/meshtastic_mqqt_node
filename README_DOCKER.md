# Docker Setup для Meshtastic MQTT Node Simulator

## Быстрый старт

### Запуск с Docker Compose

Docker Compose включает два сервиса:
- **mosquitto** - MQTT брокер (порт 1883)
- **meshtastic-node** - Meshtastic MQTT Node Simulator (порт 4403)

```bash
# Создание password файла для Mosquitto (если еще не создан)
# См. mosquitto/README.md для инструкций

# Запуск в фоне
docker-compose up -d

# Просмотр логов всех сервисов
docker-compose logs -f

# Просмотр логов только meshtastic-node
docker-compose logs -f meshtastic-node

# Просмотр логов только mosquitto
docker-compose logs -f mosquitto

# Остановка
docker-compose down
```

### Запуск отдельного контейнера

```bash
# Сборка образа
docker build -t meshtastic-node .

# Запуск контейнера
docker run -d \
  --name meshtastic-node \
  -p 4403:4403 \
  -v $(pwd)/config:/app/config:rw \
  -v $(pwd)/logs:/app/logs:rw \
  -v $(pwd)/data:/app/data:rw \
  meshtastic-node
```

## Структура volumes

### Meshtastic Node
- `./config` - конфигурационные файлы (project.yaml, node_defaults.json, nodes/)
- `./logs` - логи приложения (simulator.log)
- `./data` - данные узлов (опционально)

### Mosquitto
- `./mosquitto/config` - конфигурация Mosquitto (mosquitto.conf, passwd)
- `./mosquitto/logs` - логи Mosquitto (mosquitto.log)
- `./mosquitto/data` - данные персистентности Mosquitto

## Настройка

### Meshtastic Node

Все настройки находятся в `config/project.yaml`. Изменения в конфигурации требуют перезапуска контейнера:

```bash
docker-compose restart meshtastic-node
```

### Mosquitto

Конфигурация Mosquitto находится в `mosquitto/config/mosquitto.conf`.

**Важно**: Перед первым запуском создайте password файл:

```bash
# Вариант 1: Используя Docker контейнер
docker exec -it meshtastic-mosquitto mosquitto_passwd -c /mosquitto/config/passwd username

# Вариант 2: Если Mosquitto установлен локально
mosquitto_passwd -c mosquitto/config/passwd username
```

Подробнее см. `mosquitto/README.md`.

После изменения конфигурации Mosquitto:

```bash
docker-compose restart mosquitto
```

## Просмотр логов

### Логи Docker
```bash
docker-compose logs -f meshtastic-node
```

### Логи приложения (файл)
```bash
# Логи Meshtastic Node
tail -f logs/simulator.log

# Логи Mosquitto
tail -f mosquitto/logs/mosquitto.log
```

## Подключение клиента

После запуска контейнера подключитесь к ноде:

```bash
meshtastic --host localhost:4403
```

## Переменные окружения

Можно переопределить настройки MQTT через переменные окружения в `docker-compose.yml`:

```yaml
environment:
  # По умолчанию используется mosquitto из docker-compose
  - MQTT_BROKER=mosquitto
  - MQTT_PORT=1883
  - MQTT_USERNAME=username  # Должен быть создан в mosquitto/config/passwd
  - MQTT_PASSWORD=password  # Пароль, указанный при создании пользователя
```

**Примечание**: По умолчанию `meshtastic-node` использует локальный Mosquitto из docker-compose. Для использования внешнего MQTT брокера измените `MQTT_BROKER` на нужный адрес.

## Healthcheck

Оба контейнера включают healthcheck:
- **meshtastic-node**: проверяет доступность TCP порта 4403
- **mosquitto**: проверяет доступность MQTT брокера

Статус можно проверить:

```bash
docker-compose ps
```

## Troubleshooting

### Проблемы с правами доступа

Если возникают проблемы с записью в volumes, проверьте права:

```bash
# Linux/Mac
chmod -R 755 config logs data mosquitto

# Windows: обычно не требуется, но убедитесь, что директории существуют
```

### Просмотр логов контейнера

```bash
# Логи meshtastic-node
docker-compose logs --tail=100 meshtastic-node

# Логи mosquitto
docker-compose logs --tail=100 mosquitto
```

### Проблемы с Mosquitto

Если Mosquitto не запускается, проверьте:
1. Существует ли файл `mosquitto/config/passwd` (если `allow_anonymous false`)
2. Правильность пути к конфигурации в `mosquitto/config/mosquitto.conf`
3. Логи: `docker-compose logs mosquitto`

### Пересборка образа

```bash
docker-compose build --no-cache
docker-compose up -d
```


