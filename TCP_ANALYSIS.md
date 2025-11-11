# Анализ логики работы TCP в Firmware Meshtastic и сравнение с проектом

## ⚠️ ВАЖНО: Документ обновлен после рефакторинга

**Статус:** ✅ Проект теперь использует тот же подход, что и firmware.

**Дата обновления:** После переделки на state machine и неблокирующий режим

**Детальное сравнение:** См. `TCP_COMPARISON_DETAILED.md`

---

## Обзор

Данный документ описывает логику работы TCP в Firmware Meshtastic и сравнивает её с реализацией в проекте `meshtastic_node_simulator`.

**Текущее состояние:** Реализация полностью соответствует firmware подходу.

## 1. StreamAPI протокол (Framing)

### 1.1 Формат пакета

Оба проекта используют одинаковый формат framing для protobuf сообщений:

**Firmware (StreamAPI.cpp:7-9):**
```cpp
#define START1 0x94
#define START2 0xc3
#define HEADER_LEN 4
```

**Проект (config.py:18-20):**
```python
START1 = 0x94
START2 = 0xC3
HEADER_LEN = 4
```

Формат заголовка: `[START1][START2][LENGTH_HI][LENGTH_LO]` (big-endian, 16-bit длина)

### 1.2 Отправка пакетов

**Firmware (StreamAPI.cpp:171-183):**
```cpp
void StreamAPI::emitTxBuffer(size_t len)
{
    if (len != 0) {
        txBuf[0] = START1;
        txBuf[1] = START2;
        txBuf[2] = (len >> 8) & 0xff;
        txBuf[3] = len & 0xff;
        auto totalLen = len + HEADER_LEN;
        stream->write(txBuf, totalLen);
        stream->flush();
    }
}
```

**Проект (stream_api.py:16-21):**
```python
@staticmethod
def add_framing(payload: bytes) -> bytes:
    if len(payload) > MAX_TO_FROM_RADIO_SIZE:
        raise ValueError(f"Payload слишком большой: {len(payload)} > {MAX_TO_FROM_RADIO_SIZE}")
    header = struct.pack('>BBH', START1, START2, len(payload))
    return header + payload
```

✅ **Сходство:** Оба используют одинаковый формат framing (0x94C3 + длина).

## 2. Парсинг входящих пакетов

### 2.1 Firmware: State Machine подход

**Firmware (StreamAPI.cpp:59-106):**
- Использует **state machine** с указателем `rxPtr`
- Поиск START1/START2 по одному байту
- При ошибке: `rxPtr = 0` (сброс состояния)
- Хранит весь пакет в буфере `rxBuf[ptr]`

```cpp
int32_t StreamAPI::handleRecStream(char *buf, uint16_t bufLen)
{
    uint16_t index = 0;
    while (bufLen > index) {
        uint8_t c = (uint8_t)buf[index++];
        size_t ptr = rxPtr;
        rxPtr++;
        rxBuf[ptr] = c;
        
        if (ptr == 0) { // looking for START1
            if (c != START1)
                rxPtr = 0; // failed to find framing
        } else if (ptr == 1) { // looking for START2
            if (c != START2)
                rxPtr = 0; // failed to find framing
        } else if (ptr >= HEADER_LEN - 1) {
            uint32_t len = (rxBuf[2] << 8) + rxBuf[3];
            if (ptr == HEADER_LEN - 1) {
                if (len > MAX_TO_FROM_RADIO_SIZE)
                    rxPtr = 0; // length is bogus
            }
            if (rxPtr != 0 && ptr + 1 >= len + HEADER_LEN) {
                rxPtr = 0; // start over
                handleToRadio(rxBuf + HEADER_LEN, len);
            }
        }
    }
    return 0;
}
```

### 2.2 Проект: Буферный подход

**Проект (tcp/server.py:359-373):**
- Использует **буфер** `rx_buffer` для накопления данных
- Поиск START1/START2 в буфере
- При ошибке: сдвиг буфера на 1 байт (`rx_buffer = rx_buffer[1:]`)
- Извлечение payload после полного заголовка

```python
while len(rx_buffer) >= HEADER_LEN:
    if rx_buffer[0] != START1 or rx_buffer[1] != START2:
        rx_buffer = rx_buffer[1:]  # Сдвиг на 1 байт
        continue
    
    length = struct.unpack('>H', rx_buffer[2:4])[0]
    if length > MAX_TO_FROM_RADIO_SIZE:
        rx_buffer = rx_buffer[1:]
        continue
    
    if len(rx_buffer) < HEADER_LEN + length:
        break  # Недостаточно данных
    
    payload = rx_buffer[HEADER_LEN:HEADER_LEN + length]
    rx_buffer = rx_buffer[HEADER_LEN + length:]
    session._handle_to_radio(payload)
```

### 2.3 Сравнение подходов

| Аспект | Firmware | Проект |
|-------|----------|--------|
| **Подход** | State machine с `rxPtr` | Буфер с поиском |
| **Обработка ошибок** | Сброс `rxPtr = 0` | Сдвиг буфера на 1 байт |
| **Память** | Фиксированный буфер `rxBuf` | Динамический `rx_buffer` |
| **Производительность** | O(1) доступ по индексу | O(n) поиск в буфере |
| **Сложность** | Более сложная логика | Более простая логика |

⚠️ **Различие:** Firmware использует state machine, проект - буферный поиск. Оба подхода корректны, но state machine более эффективен для встраиваемых систем.

## 3. Чтение из TCP сокета

### 3.1 Firmware: Неблокирующий режим

**Firmware (StreamAPI.cpp:111-165):**
- Проверка `stream->available()` перед чтением
- Неблокирующий режим
- Адаптивный polling: 5ms (если недавно были данные) или 250ms (если нет активности)
- Отслеживание `lastRxMsec` для оптимизации

```cpp
int32_t StreamAPI::readStream()
{
    if (!stream->available()) {
        bool recentRx = Throttle::isWithinTimespanMs(lastRxMsec, 2000);
        return recentRx ? 5 : 250;  // Адаптивный polling
    } else {
        while (stream->available()) {
            int cInt = stream->read();
            if (cInt < 0) break;
            // ... обработка байта
        }
        lastRxMsec = millis();
        return 0;
    }
}
```

### 3.2 Проект: Блокирующий режим с таймаутом

**Проект (tcp/server.py:349-403):**
- Блокирующий `recv(4096)` с таймаутом 1 секунда
- Обработка таймаута как нормального события
- Чтение до 4096 байт за раз

```python
try:
    data = session.client_socket.recv(4096)
    if not data:
        # Клиент закрыл соединение
        break
    rx_buffer += data
    # ... обработка буфера
except socket.timeout:
    # Таймаут - это нормально, продолжаем работу
    pass
```

### 3.3 Android клиент: Блокирующий с таймаутом

**Android (TCPInterface.kt:107-136):**
- `socket.soTimeout = 500` мс
- Блокирующий `inputStream.read()` с таймаутом
- Закрытие соединения после 90 секунд неактивности (180 таймаутов × 500мс)

```kotlin
socket.tcpNoDelay = true
socket.soTimeout = 500

var timeoutCount = 0
while (timeoutCount < 180) {
    try {
        val c = inputStream.read()
        if (c == -1) break
        timeoutCount = 0
        readChar(c.toByte())
    } catch (ex: SocketTimeoutException) {
        timeoutCount++
    }
}
```

### 3.4 Сравнение подходов

| Аспект | Firmware | Проект | Android клиент |
|-------|----------|--------|----------------|
| **Режим** | Неблокирующий | Блокирующий с таймаутом | Блокирующий с таймаутом |
| **Таймаут** | Адаптивный (5-250ms) | Фиксированный (1000ms) | Фиксированный (500ms) |
| **Размер чтения** | По 1 байту | До 4096 байт | По 1 байту |
| **Закрытие при неактивности** | `checkConnectionTimeout()` | Нет (keepalive) | 90 секунд |

⚠️ **Различие:** Firmware использует неблокирующий режим для экономии CPU, проект и Android используют блокирующий режим с таймаутом.

## 4. Запись в TCP сокет

### 4.1 Firmware: Отправка всех доступных пакетов

**Firmware (StreamAPI.cpp:47-57):**
- `writeStream()` вызывается в основном цикле
- Отправляет все доступные пакеты из очереди за один вызов
- Использует `getFromRadio()` для получения пакетов

```cpp
void StreamAPI::writeStream()
{
    if (canWrite) {
        uint32_t len;
        do {
            len = getFromRadio(txBuf + HEADER_LEN);
            emitTxBuffer(len);
        } while (len);  // Отправляет все доступные пакеты
    }
}
```

### 4.2 Проект: Ограниченная отправка в основном цикле

**Проект (tcp/server.py:426-456):**
- Отправка пакетов из MQTT очереди в основном цикле
- Ограничение: максимум 10 пакетов за итерацию
- Предотвращает блокировку чтения

```python
if session.mqtt_client and hasattr(session.mqtt_client, 'to_client_queue'):
    max_packets_per_iteration = 10
    packets_sent = 0
    while not session.mqtt_client.to_client_queue.empty() and packets_sent < max_packets_per_iteration:
        response = session.mqtt_client.to_client_queue.get_nowait()
        session.client_socket.send(response)
        packets_sent += 1
```

✅ **Сходство:** Оба отправляют пакеты в основном цикле, но проект ограничивает количество для предотвращения блокировки.

## 5. Настройки TCP сокета

### 5.1 Firmware: Настройки через Stream

Firmware использует абстракцию `Stream`, которая может быть реализована для TCP, Serial и т.д. Настройки TCP зависят от конкретной реализации.

### 5.2 Проект: Явные настройки сокета

**Проект (tcp/server.py:318-334):**
```python
# SO_KEEPALIVE - для обнаружения разорванных соединений
session.client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

# TCP_NODELAY - отключает алгоритм Nagle (уменьшение задержки)
session.client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

# Таймаут для recv - 1 секунда
session.client_socket.settimeout(1.0)
```

### 5.3 Android клиент: Аналогичные настройки

**Android (TCPInterface.kt:108-109):**
```kotlin
socket.tcpNoDelay = true  // TCP_NODELAY
socket.soTimeout = 500    // Таймаут 500мс
```

✅ **Сходство:** Проект и Android используют одинаковые настройки (`TCP_NODELAY`, таймаут), что соответствует лучшим практикам для низкой задержки.

## 6. Обработка ошибок

### 6.1 Firmware: Сброс состояния при ошибке парсинга

**Firmware (StreamAPI.cpp:78-83):**
```cpp
if (ptr == 0) {
    if (c != START1)
        rxPtr = 0;  // Сброс состояния, поиск заново
}
```

Ошибки парсинга не закрывают соединение, просто сбрасывают состояние парсера.

### 6.2 Проект: Игнорирование ошибок парсинга

**Проект (tcp/server.py:375-387):**
```python
try:
    session._handle_to_radio(payload)
except Exception as e:
    # Ошибка обработки пакета - логируем, но не закрываем соединение
    # (как в firmware StreamAPI - ошибки парсинга просто сбрасывают rxPtr)
    error("TCP", f"Error processing packet (ignoring): {e}")
    # Продолжаем работу
```

✅ **Сходство:** Оба подхода не закрывают соединение при ошибках парсинга, что позволяет восстановиться после поврежденных пакетов.

## 7. Управление соединением

### 7.1 Firmware: Проверка таймаута соединения

**Firmware (StreamAPI.cpp:15):**
```cpp
int32_t StreamAPI::runOncePart()
{
    auto result = readStream();
    writeStream();
    checkConnectionTimeout();  // Проверка таймаута
    return result;
}
```

Firmware проверяет таймаут соединения в основном цикле.

### 7.2 Проект: Keepalive через телеметрию

**Проект (tcp/server.py:414-421):**
```python
# Периодическая отправка телеметрии через TCP для поддержания активности соединения
# (Android клиент закрывает соединение после 90 секунд неактивности)
KEEPALIVE_INTERVAL = 60.0  # секунд

if current_time - last_keepalive_send >= KEEPALIVE_INTERVAL:
    last_keepalive_send = current_time
    session._send_telemetry_keepalive()
```

⚠️ **Различие:** Проект использует явный keepalive через телеметрию каждые 60 секунд, чтобы предотвратить закрытие соединения Android клиентом (90 секунд неактивности).

## 8. Основной цикл обработки

### 8.1 Firmware: Адаптивный polling

**Firmware (StreamAPI.cpp:11-17):**
```cpp
int32_t StreamAPI::runOncePart()
{
    auto result = readStream();      // Возвращает задержку до следующего вызова
    writeStream();
    checkConnectionTimeout();
    return result;  // 0 = немедленно, 5-250ms = задержка
}
```

Firmware возвращает задержку до следующего вызова, оптимизируя использование CPU.

### 8.2 Проект: Синхронный цикл с таймаутом

**Проект (tcp/server.py:343-463):**
```python
while self.running:
    current_time = time.time()
    
    # Чтение из сокета (блокирующее с таймаутом 1с)
    try:
        data = session.client_socket.recv(4096)
        # ... обработка
    except socket.timeout:
        pass  # Таймаут - нормально
    
    # Периодические задачи
    if current_time - last_position_check >= 5.0:
        session._run_position_broadcast()
    
    if current_time - last_keepalive_send >= KEEPALIVE_INTERVAL:
        session._send_telemetry_keepalive()
    
    # Отправка пакетов из MQTT
    # ...
```

⚠️ **Различие:** Firmware использует адаптивный polling для экономии CPU, проект использует синхронный цикл с блокирующим чтением.

## 9. Выводы и рекомендации

### 9.1 Сходства

1. ✅ **Формат framing:** Одинаковый (0x94C3 + длина)
2. ✅ **Обработка ошибок:** Оба не закрывают соединение при ошибках парсинга
3. ✅ **Настройки сокета:** TCP_NODELAY, keepalive
4. ✅ **Отправка пакетов:** В основном цикле

### 9.2 Различия (ОБНОВЛЕНО)

1. ✅ **Парсинг:** Теперь используется state machine (как в firmware) через `StreamAPIStateMachine`
2. ✅ **Режим чтения:** Переведен на неблокирующий режим с адаптивным polling (как в firmware)
3. ✅ **Управление CPU:** Реализован адаптивный polling (5-250ms) для экономии CPU (как в firmware)
4. ⚠️ **Keepalive:** Проект дополнительно использует явную отправку телеметрии каждые 60 секунд (для совместимости с Android клиентом, который закрывает соединение после 90 секунд неактивности)

### 9.3 Рекомендации

✅ **РЕАЛИЗОВАНО:**
1. ✅ **State machine парсинг:** Реализован `StreamAPIStateMachine` класс для парсинга (как в firmware)
2. ✅ **Неблокирующий режим:** Переведен на неблокирующий режим с `select` для эффективного polling
3. ✅ **Адаптивный polling:** Реализован адаптивный polling (5-250ms) для экономии CPU
4. ✅ **Таймаут соединения:** Добавлена проверка таймаута соединения `_check_connection_timeout()` (15 минут, как в firmware)

**Изменения:**
- `meshtastic_simulator/protocol/stream_api.py`: Добавлен класс `StreamAPIStateMachine` для state machine парсинга
- `meshtastic_simulator/tcp/server.py`: Переделан `_handle_client_session` на неблокирующий режим с адаптивным polling
- Реализованы методы `_read_stream()`, `_write_stream()`, `_check_connection_timeout()` по аналогии с firmware

## 10. Ссылки на код

### Firmware
- `examples/firmware/src/mesh/StreamAPI.cpp` - основная логика StreamAPI
- `examples/firmware/src/mesh/StreamAPI.h` - определение класса

### Проект
- `meshtastic_simulator/tcp/server.py` - TCP сервер
- `meshtastic_simulator/tcp/session.py` - сессия TCP клиента
- `meshtastic_simulator/protocol/stream_api.py` - StreamAPI протокол

### Android клиент
- `examples/Meshtastic-Android/app/src/main/java/com/geeksville/mesh/repository/radio/TCPInterface.kt` - TCP клиент Android


