# Детальное сравнение TCP логики: Firmware vs Проект (ОБНОВЛЕНО)

## Статус: ✅ Реализация соответствует firmware

После рефакторинга проект использует тот же подход, что и firmware.

---

## 1. Архитектура основного цикла

### Firmware (StreamAPI.cpp:11-16)
```cpp
int32_t StreamAPI::runOncePart()
{
    auto result = readStream();      // Чтение данных
    writeStream();                   // Отправка пакетов
    checkConnectionTimeout();        // Проверка таймаута
    return result;                   // Задержка до следующего вызова
}
```

### Проект (server.py:352-365)
```python
while self.running:
    current_time_ms = int(time.time() * 1000)
    
    # Чтение данных (как readStream())
    delay_ms = self._read_stream(session, state_machine, current_time_ms, RECENT_RX_THRESHOLD_MS)
    
    # Отправка пакетов (как writeStream())
    self._write_stream(session)
    
    # Проверка таймаута (как checkConnectionTimeout())
    if self._check_connection_timeout(session, state_machine, current_time_ms, SERIAL_CONNECTION_TIMEOUT_MS):
        break
```

✅ **Соответствие:** Идентичная структура - read → write → check timeout

---

## 2. Парсинг входящих пакетов (State Machine)

### Firmware (StreamAPI.cpp:59-106)
```cpp
int32_t StreamAPI::handleRecStream(char *buf, uint16_t bufLen)
{
    uint16_t index = 0;
    while (bufLen > index) {
        uint8_t c = (uint8_t)buf[index++];
        size_t ptr = rxPtr;
        
        rxPtr++;        // assume we will probably advance
        rxBuf[ptr] = c; // store all bytes
        
        if (ptr == 0) { // looking for START1
            if (c != START1)
                rxPtr = 0; // failed to find framing
        } else if (ptr == 1) { // looking for START2
            if (c != START2)
                rxPtr = 0; // failed to find framing
        } else if (ptr >= HEADER_LEN - 1) {
            uint32_t len = (rxBuf[2] << 8) + rxBuf[3]; // big endian
            
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

### Проект (stream_api.py:57-105)
```python
def handle_rec_stream(self, buf: bytes) -> None:
    index = 0
    while index < len(buf):
        c = buf[index]
        index += 1
        
        ptr = self.rx_ptr
        self.rx_ptr += 1
        self.rx_buf[ptr] = c
        
        if ptr == 0:  # Ищем START1
            if c != START1:
                self.rx_ptr = 0
        elif ptr == 1:  # Ищем START2
            if c != START2:
                self.rx_ptr = 0
        elif ptr >= HEADER_LEN - 1:
            length = (self.rx_buf[2] << 8) + self.rx_buf[3]
            
            if ptr == HEADER_LEN - 1:
                if length > MAX_TO_FROM_RADIO_SIZE:
                    self.rx_ptr = 0
            
            if self.rx_ptr != 0 and ptr + 1 >= length + HEADER_LEN:
                self.rx_ptr = 0
                payload = bytes(self.rx_buf[HEADER_LEN:HEADER_LEN + length])
                self.handle_to_radio(payload)
```

✅ **Соответствие:** Идентичная логика state machine с указателем `rxPtr`

---

## 3. Чтение из потока (Адаптивный Polling)

### Firmware (StreamAPI.cpp:30-42)
```cpp
int32_t StreamAPI::readStream(char *buf, uint16_t bufLen)
{
    if (bufLen < 1) {
        // Nothing available - адаптивный polling
        bool recentRx = Throttle::isWithinTimespanMs(lastRxMsec, 2000);
        return recentRx ? 5 : 250;  // 5ms или 250ms
    } else {
        handleRecStream(buf, bufLen);
        lastRxMsec = millis();
        return 0;  // Немедленно продолжить
    }
}
```

### Проект (server.py:411-460)
```python
def _read_stream(self, session, state_machine, current_time_ms, recent_threshold_ms) -> int:
    POLL_INTERVAL_RECENT_MS = 5   # 5ms если недавно были данные
    POLL_INTERVAL_IDLE_MS = 250   # 250ms если нет активности
    
    try:
        data = session.client_socket.recv(4096)  # Неблокирующий режим
        if not data:
            raise ConnectionResetError("Client closed connection")
        
        state_machine.handle_rec_stream(data)
        return 0  # Немедленно продолжить
        
    except BlockingIOError:
        # Нет данных - адаптивный polling
        last_rx_ms = state_machine.get_last_rx_msec()
        if last_rx_ms > 0:
            time_since_last = current_time_ms - last_rx_ms
            if time_since_last < recent_threshold_ms:  # 2000ms
                return POLL_INTERVAL_RECENT_MS  # 5ms
            else:
                return POLL_INTERVAL_IDLE_MS    # 250ms
        else:
            return POLL_INTERVAL_IDLE_MS
```

✅ **Соответствие:** 
- Неблокирующий режим
- Адаптивный polling: 5ms (активность) / 250ms (idle)
- Порог "недавно": 2000ms
- Возврат 0 при наличии данных

---

## 4. Отправка пакетов

### Firmware (StreamAPI.cpp:47-57)
```cpp
void StreamAPI::writeStream()
{
    if (canWrite) {
        uint32_t len;
        do {
            len = getFromRadio(txBuf + HEADER_LEN);
            emitTxBuffer(len);
        } while (len);  // Отправляет ВСЕ доступные пакеты
    }
}
```

### Проект (server.py:462-516)
```python
def _write_stream(self, session) -> None:
    if not session.mqtt_client or not hasattr(session.mqtt_client, 'to_client_queue'):
        return
    
    packets_sent = 0
    max_packets_per_iteration = 50  # Лимит для предотвращения блокировки
    
    while not session.mqtt_client.to_client_queue.empty() and packets_sent < max_packets_per_iteration:
        response = session.mqtt_client.to_client_queue.get_nowait()
        # ... обработка и отправка
        session.client_socket.send(response)
        packets_sent += 1
```

⚠️ **Небольшое отличие:** Проект ограничивает количество пакетов (50) для предотвращения блокировки, но это разумно для Python реализации.

---

## 5. Проверка таймаута соединения

### Firmware (PhoneAPI.cpp:121-132)
```cpp
bool PhoneAPI::checkConnectionTimeout()
{
    if (isConnected()) {
        bool newContact = checkIsConnected();
        if (!newContact) {
            LOG_INFO("Lost phone connection");
            close();
            return true;
        }
    }
    return false;
}
```

### Firmware (SerialModule.cpp:129)
```cpp
// Таймаут: 15 минут
#define SERIAL_CONNECTION_TIMEOUT (15 * 60) * 1000UL
return Throttle::isWithinTimespanMs(lastContactMsec, SERIAL_CONNECTION_TIMEOUT);
```

### Проект (server.py:518-533)
```python
def _check_connection_timeout(self, session, state_machine, current_time_ms, timeout_ms) -> bool:
    SERIAL_CONNECTION_TIMEOUT_MS = 15 * 60 * 1000  # 15 минут
    
    last_rx_ms = state_machine.get_last_rx_msec()
    
    if last_rx_ms > 0:
        time_since_last = current_time_ms - last_rx_ms
        if time_since_last > timeout_ms:
            info("TCP", f"Connection timeout ({time_since_last}ms > {timeout_ms}ms)")
            return True
    
    return False
```

✅ **Соответствие:** 
- Таймаут: 15 минут (как в firmware)
- Проверка на основе `lastRxMsec` / `last_rx_msec`
- Закрытие соединения при таймауте

---

## 6. Настройки TCP сокета

### Firmware
Настройки зависят от конкретной реализации Stream (TCP/Serial).

### Проект (server.py:320-327)
```python
# SO_KEEPALIVE - для обнаружения разорванных соединений
session.client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

# TCP_NODELAY - отключает алгоритм Nagle (уменьшение задержки)
session.client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

# Неблокирующий режим (как в firmware)
session.client_socket.setblocking(False)
```

✅ **Соответствие:** Правильные настройки для низкой задержки и неблокирующего режима

---

## 7. Обработка ошибок

### Firmware
- Ошибки парсинга: сброс `rxPtr = 0`, продолжение работы
- Ошибки соединения: закрытие соединения

### Проект
```python
try:
    payload = bytes(self.rx_buf[HEADER_LEN:HEADER_LEN + length])
    self.handle_to_radio(payload)
except Exception as e:
    # Ошибка обработки пакета - логируем, но не закрываем соединение
    # (как в firmware - ошибки парсинга просто сбрасывают rxPtr)
    error("StreamAPI", f"Error processing packet (ignoring): {e}")
```

✅ **Соответствие:** Ошибки парсинга не закрывают соединение

---

## 8. Константы и параметры

| Параметр | Firmware | Проект | Соответствие |
|----------|----------|--------|--------------|
| **START1** | 0x94 | 0x94 | ✅ |
| **START2** | 0xC3 | 0xC3 | ✅ |
| **HEADER_LEN** | 4 | 4 | ✅ |
| **MAX_TO_FROM_RADIO_SIZE** | 512 | 512 | ✅ |
| **Polling (активность)** | 5ms | 5ms | ✅ |
| **Polling (idle)** | 250ms | 250ms | ✅ |
| **Порог "недавно"** | 2000ms | 2000ms | ✅ |
| **Таймаут соединения** | 15 минут | 15 минут | ✅ |

✅ **Все константы соответствуют firmware**

---

## 9. Сравнительная таблица реализации

| Аспект | Firmware | Проект | Статус |
|--------|----------|--------|--------|
| **Парсинг** | State machine с `rxPtr` | State machine с `rx_ptr` | ✅ Идентично |
| **Режим чтения** | Неблокирующий | Неблокирующий | ✅ Идентично |
| **Polling** | Адаптивный (5-250ms) | Адаптивный (5-250ms) | ✅ Идентично |
| **Отправка** | Все доступные пакеты | До 50 пакетов за раз | ⚠️ Небольшое отличие |
| **Таймаут** | 15 минут | 15 минут | ✅ Идентично |
| **Обработка ошибок** | Сброс состояния | Сброс состояния | ✅ Идентично |
| **Структура цикла** | read → write → timeout | read → write → timeout | ✅ Идентично |

---

## 10. Выводы

### ✅ Полное соответствие firmware:

1. **State Machine парсинг** - идентичная реализация
2. **Неблокирующий режим** - полностью соответствует
3. **Адаптивный polling** - те же параметры (5-250ms)
4. **Проверка таймаута** - 15 минут, как в firmware
5. **Структура цикла** - read → write → check timeout
6. **Обработка ошибок** - сброс состояния без закрытия соединения

### ⚠️ Небольшие отличия (оправданные):

1. **Лимит отправки пакетов** - 50 пакетов за раз (вместо всех) для предотвращения блокировки в Python
2. **Дополнительный keepalive** - отправка телеметрии каждые 60 секунд для совместимости с Android клиентом

### 🎯 Результат:

**Реализация TCP в проекте полностью соответствует подходу firmware** и использует те же алгоритмы и параметры. Код оптимизирован для работы в Python окружении, сохраняя при этом логику и поведение оригинальной firmware реализации.

---

## 11. Файлы реализации

### Firmware
- `examples/firmware/src/mesh/StreamAPI.cpp` - основная логика
- `examples/firmware/src/mesh/StreamAPI.h` - определение класса
- `examples/firmware/src/mesh/PhoneAPI.cpp` - checkConnectionTimeout

### Проект
- `meshtastic_simulator/protocol/stream_api.py` - StreamAPIStateMachine
- `meshtastic_simulator/tcp/server.py` - TCP сервер с firmware-подходом

---

**Дата обновления:** После рефакторинга на подход firmware  
**Статус:** ✅ Полное соответствие

