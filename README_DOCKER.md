# Docker Setup для Meshtastic MQTT Node Simulator

## Быстрый старт

### Запуск с Docker Compose

```bash
# Запуск в фоне
docker-compose up -d

# Просмотр логов
docker-compose logs -f

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

- `./config` - конфигурационные файлы (project.yaml, node_defaults.json, nodes/)
- `./logs` - логи приложения (simulator.log)
- `./data` - данные узлов (опционально)

## Настройка

Все настройки находятся в `config/project.yaml`. Изменения в конфигурации требуют перезапуска контейнера:

```bash
docker-compose restart
```

## Просмотр логов

### Логи Docker
```bash
docker-compose logs -f meshtastic-node
```

### Логи приложения (файл)
```bash
tail -f logs/simulator.log
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
  - MQTT_BROKER=mqtt.meshtastic.org
  - MQTT_PORT=1883
  - MQTT_USERNAME=meshdev
  - MQTT_PASSWORD=large4cats
```

## Healthcheck

Контейнер включает healthcheck, который проверяет доступность TCP порта 4403. Статус можно проверить:

```bash
docker-compose ps
```

## Troubleshooting

### Проблемы с правами доступа

Если возникают проблемы с записью в volumes, проверьте права:

```bash
chmod -R 755 config logs data
```

### Просмотр логов контейнера

```bash
docker-compose logs --tail=100 meshtastic-node
```

### Пересборка образа

```bash
docker-compose build --no-cache
docker-compose up -d
```


