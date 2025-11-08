# Механизм отчетов о доставке сообщений (Delivery Reports)

## Обзор

В Meshtastic используется механизм ACK/NAK для подтверждения доставки сообщений. Когда пакет отправляется с флагом `want_ack=true`, получатель должен отправить обратно ACK (подтверждение) или NAK (отказ) пакет.

## Как это работает в firmware

### 1. Отправка пакета с want_ack=true

Когда клиент отправляет пакет с `want_ack=true`:
- Пакет отправляется получателю
- Отправитель ожидает ACK/NAK пакет
- Если ACK/NAK не получен, пакет может быть переотправлен

### 2. Получение пакета с want_ack=true

Когда получатель получает пакет с `want_ack=true`:
- Пакет обрабатывается через `ReliableRouter::sniffReceived()`
- Проверяется условие: `if (isToUs(p) && p->want_ack && payload_type == 'decoded')`
- Если условие выполнено, вызывается `sendAckNak()` для отправки ACK/NAK

### 3. Создание ACK/NAK пакета

ACK/NAK пакет создается через `MeshModule::allocAckNak()`:

```cpp
meshtastic_MeshPacket *MeshModule::allocAckNak(
    meshtastic_Routing_Error err,  // Routing_Error_NONE для ACK, другая ошибка для NAK
    NodeNum to,                    // node_num отправителя исходного пакета
    PacketId idFrom,               // ID исходного пакета
    ChannelIndex chIndex,          // Индекс канала
    uint8_t hopLimit               // Hop limit для ответа
)
```

**Структура ACK/NAK пакета:**
- `portnum = ROUTING_APP`
- `request_id = ID исходного пакета` (критически важно!)
- `decoded.payload` содержит `Routing` protobuf с `error_reason`
- `to = from исходного пакета`
- `from = наш node_num`
- `hop_limit` может быть 0 (для прямых сообщений) или вычисляется на основе hop_start/hop_limit исходного пакета
- `priority = ACK`
- `want_ack` может быть `true` для текстовых сообщений (для надежной доставки подтверждения)

### 4. Обработка ACK/NAK клиентом

Клиент получает ACK/NAK пакет и:
- Проверяет `portnum == ROUTING_APP` и `request_id != 0`
- Декодирует `Routing` protobuf из `decoded.payload`
- Проверяет `error_reason`:
  - `Routing_Error_NONE` = ACK (доставлено успешно)
  - Любая другая ошибка = NAK (доставка не удалась)
- Может определить количество hops по `hop_start` и `hop_limit`

## Особенности

### Broadcast сообщения

Для broadcast сообщений (`to = 0xFFFFFFFF`):
- ACK не отправляется автоматически
- Но если broadcast сообщение получено и обработано, может быть отправлен ACK от конкретного получателя

### Текстовые сообщения

Для текстовых сообщений (TEXT_MESSAGE_APP):
- ACK может быть отправлен с `want_ack=true` для надежной доставки подтверждения
- Это реализовано в `ReliableRouter::shouldSuccessAckWithWantAck()`

### Пакеты через MQTT

В firmware, когда пакет приходит через MQTT:
- Пакет передается в router через `router->enqueueReceivedMessage()`
- Router проверяет `want_ack` и отправляет ACK/NAK через `sendAckNak()`
- ACK/NAK отправляется обратно отправителю через MQTT

## Реализация в симуляторе

### Текущая реализация

1. **Пакеты от TCP клиента:**
   - Обрабатываются в `TCPConnectionSession._handle_mesh_packet()`
   - Проверяется `want_ack` через `PacketHandler.should_send_ack()`
   - ACK отправляется клиенту через `_send_ack()`

2. **Пакеты через MQTT:**
   - Обрабатываются в `MQTTPacketProcessor.process_mqtt_message()`
   - **ИСПРАВЛЕНО:** Добавлена проверка `want_ack` и отправка ACK/NAK обратно отправителю через MQTT
   - ACK отправляется только если:
     - Пакет адресован нам (не broadcast)
     - Это не ACK сам по себе (не Routing сообщение с request_id)
     - Это не Admin пакет (Admin пакеты обрабатываются отдельно)

### Логика отправки ACK/NAK

1. **Проверка условий:**
   - `payload_type == 'decoded'` - только расшифрованные пакеты
   - `packet.want_ack == True` - пакет запрашивает подтверждение
   - `is_to_us == True` - пакет адресован нам (не broadcast)
   - `not is_routing_ack` - это не ACK сам по себе
   - `not is_admin` - это не Admin пакет

2. **Создание ACK пакета:**
   - Используется `PacketHandler.create_ack_packet()`
   - `request_id = ID исходного пакета` (критически важно!)
   - `error_reason = Routing_Error_NONE` (ACK)
   - `hop_limit = 0` (для прямых сообщений через MQTT)

3. **Отправка через MQTT:**
   - Находится сессия получателя (наша сессия)
   - ACK публикуется в MQTT через `mqtt_client.publish_packet()`
   - Используется тот же канал, что и исходный пакет

