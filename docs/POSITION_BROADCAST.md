# Логика отправки местоположения в Meshtastic Firmware

## Обзор

В firmware отправка местоположения реализована в модуле `PositionModule` (`src/modules/PositionModule.cpp`). Пакеты позиции отправляются через порт `POSITION_APP` (portnum=3) в виде protobuf сообщения `Position`.

## Основные компоненты

### 1. Создание пакета позиции (`allocPositionPacket`)

**Условия для создания пакета:**
- `precision != 0` (если precision=0, отправка пропускается)
- `localPosition.latitude_i != 0 && localPosition.longitude_i != 0` (если координаты нулевые, отправка пропускается)
- Узел должен иметь валидную позицию (`node->has_position == true`)

**Обязательные поля (всегда включаются):**
- `latitude_i` - широта (в 1e-7 градусах)
- `longitude_i` - долгота (в 1e-7 градусах)
- `precision_bits` - точность позиции (0-32 бита)
- `has_latitude_i = true`
- `has_longitude_i = true`

**Условные поля (включаются на основе `config.position.position_flags`):**

| Флаг | Значение | Поле в пакете |
|------|----------|---------------|
| `ALTITUDE` (0x0001) | Высота | `altitude` (MSL) или `altitude_hae` (HAE) |
| `ALTITUDE_MSL` (0x0002) | Использовать MSL вместо HAE | `altitude` вместо `altitude_hae` |
| `GEOIDAL_SEPARATION` (0x0004) | Геоидальное разделение | `altitude_geoidal_separation` |
| `DOP` (0x0008) | DOP значение | `PDOP` (по умолчанию) или `HDOP`/`VDOP` |
| `HVDOP` (0x0010) | Отдельные HDOP/VDOP | `HDOP` и `VDOP` вместо `PDOP` |
| `SATINVIEW` (0x0020) | Количество спутников | `sats_in_view` |
| `SEQ_NO` (0x0040) | Порядковый номер | `seq_number` (инкрементируется при каждой отправке) |
| `TIMESTAMP` (0x0080) | Временная метка GPS | `timestamp` |
| `HEADING` (0x0100) | Направление движения | `ground_track` |
| `SPEED` (0x0200) | Скорость движения | `ground_speed` |

**Время (`time`):**
- Всегда включается, если доступно:
  - Приоритет 1: NTP/GPS время (`RTCQualityNTP`)
  - Приоритет 2: RTC время (`RTCQualityDevice`)
  - Если качество времени низкое, `time = 0`

**Источник местоположения (`location_source`):**
- Если `config.position.fixed_position == true`: `LOC_MANUAL`
- Иначе: берется из `localPosition.location_source`

**Точность позиции (`precision`):**
- Если `precision < 32 && precision > 0`: координаты обрезаются до указанной точности
- Координаты сдвигаются к центру возможной области (не к краю)

### 2. Отправка позиции (`sendOurPosition`)

**Основной метод:**
```cpp
void PositionModule::sendOurPosition()
```

**Логика:**
1. Определяет, нужно ли запрашивать ответы (`wantReplies`):
   - `requestReplies = (currentGeneration != radioGeneration)`
   - Если поколение радио изменилось, запрашивает ответы от других узлов

2. Ищет канал с включенной отправкой позиции:
   - Перебирает каналы 0-7
   - Ищет канал с `position_precision != 0`
   - Отправляет на найденный канал

3. Вызывает `sendOurPosition(dest, wantReplies, channel)`:
   - `dest = NODENUM_BROADCAST` (широковещательная отправка)
   - Отменяет предыдущий неотправленный пакет позиции (если есть)
   - Создает пакет через `allocPositionPacket()`
   - Устанавливает:
     - `p->to = dest`
     - `p->decoded.want_response = false` (для TRACKER) или `wantReplies` (для других)
     - `p->priority = RELIABLE` (для TRACKER/TAK_TRACKER) или `BACKGROUND` (для других)
     - `p->channel = channel` (если channel > 0)
   - Отправляет через `service->sendToMesh(p, RX_SRC_LOCAL, true)`

### 3. Периодическая отправка (`runOnce`)

**Интервал выполнения:** каждые 5 секунд (`RUNONCE_INTERVAL = 5000`)

**Условия для отправки:**

1. **Проверка загрузки канала:**
   - Отправка разрешена только если загрузка канала < 25% (обычные устройства)
   - Или < 40% (для TRACKER/TAK_TRACKER)
   - Проверка: `airTime->isTxAllowedChannelUtil()`

2. **Регулярная отправка (по интервалу):**
   - Если `lastGpsSend == 0` (первая отправка) ИЛИ
   - Если прошло `intervalMs` с последней отправки:
     - `intervalMs = getConfiguredOrDefaultMsScaled(config.position.position_broadcast_secs, default_broadcast_interval_secs, numOnlineNodes)`
     - Интервал масштабируется в зависимости от количества онлайн узлов
   - Если узел имеет валидную позицию: отправляет позицию

3. **Умная отправка (smart broadcast):**
   - Если `config.position.position_broadcast_smart_enabled == true`:
     - Вычисляет расстояние, пройденное с последней отправки
     - Если расстояние превышает порог (`distanceThreshold`):
       - Проверяет минимальный интервал времени (`minimumTimeThreshold`)
       - Если прошло достаточно времени: отправляет позицию
     - Это позволяет отправлять позицию чаще при движении и реже при стоянии

**Обработка новых позиций (`handleNewPosition`):**
- Вызывается при получении новой позиции от GPS
- Использует ту же логику умной отправки (smart broadcast)
- Отправляет позицию, если пройдено достаточно расстояния и прошло достаточно времени

### 4. Обработка входящих пакетов позиции (`handleReceivedProtobuf`)

**Логика:**
1. Если пакет от нас самих (`isFromUs`):
   - Если `config.position.fixed_position == true`: игнорирует (кроме времени)
   - Иначе: обновляет локальную позицию через `nodeDB->setLocalPosition(p)`

2. Если пакет от другого узла:
   - Обновляет позицию узла через `nodeDB->updatePosition(getFrom(&mp), p)`
   - Если есть время и канал PRIMARY: пытается установить RTC время

3. Определяет `precision` из настроек канала:
   - Если канал имеет `module_settings.position_precision`: использует его
   - Если канал PRIMARY: `precision = 32` (полная точность)
   - Иначе: `precision = 0` (не отправлять позицию)

### 5. Особенности для разных ролей устройств

**TRACKER / TAK_TRACKER:**
- `want_response = false` (не запрашивает ответы)
- `priority = RELIABLE` (высокий приоритет)
- Если включен power saving: после отправки уходит в глубокий сон

**LOST_AND_FOUND:**
- После отправки позиции также отправляет текстовое сообщение с информацией

**Обычные устройства:**
- `priority = BACKGROUND` (низкий приоритет)
- Могут запрашивать ответы при смене поколения радио

## Порядок полей в пакете Position

1. **Всегда включаются:**
   - `latitude_i` (int32)
   - `longitude_i` (int32)
   - `precision_bits` (uint32)
   - `time` (uint32, если доступно)

2. **Условно включаются (по флагам):**
   - `altitude` или `altitude_hae` (если ALTITUDE)
   - `altitude_geoidal_separation` (если GEOIDAL_SEPARATION)
   - `PDOP` или `HDOP`/`VDOP` (если DOP)
   - `sats_in_view` (если SATINVIEW)
   - `timestamp` (если TIMESTAMP)
   - `seq_number` (если SEQ_NO)
   - `ground_track` (если HEADING)
   - `ground_speed` (если SPEED)

3. **Метаданные:**
   - `location_source` (источник позиции)
   - `has_*` флаги для опциональных полей

## Примеры использования

### Минимальный пакет позиции:
```cpp
Position {
  latitude_i: 374208000,      // 37.4208°
  longitude_i: -1221981000,   // -122.1981°
  precision_bits: 32,
  time: 1609459200,
  has_latitude_i: true,
  has_longitude_i: true
}
```

### Полный пакет позиции (все флаги):
```cpp
Position {
  latitude_i: 374208000,
  longitude_i: -1221981000,
  precision_bits: 32,
  time: 1609459200,
  altitude_hae: 123,
  altitude_geoidal_separation: 45,
  HDOP: 12,
  VDOP: 15,
  sats_in_view: 8,
  timestamp: 1609459200,
  seq_number: 42,
  ground_track: 180,
  ground_speed: 50,
  location_source: LOC_GPS,
  has_latitude_i: true,
  has_longitude_i: true,
  has_altitude_hae: true,
  has_altitude_geoidal_separation: true,
  has_ground_track: true,
  has_ground_speed: true
}
```

## Важные моменты для реализации в симуляторе

1. **Пакеты позиции отправляются через `POSITION_APP` (portnum=3)**
2. **Отправка происходит периодически** (по интервалу `position_broadcast_secs`)
3. **Может использоваться умная отправка** (smart broadcast) при движении
4. **Поля включаются на основе `position_flags`** (битовая маска)
5. **Точность позиции может быть ограничена** через `position_precision` канала
6. **Приоритет пакета зависит от роли устройства** (RELIABLE для TRACKER, BACKGROUND для других)
7. **Пакеты отправляются как broadcast** (`to = NODENUM_BROADCAST`)
8. **Может запрашивать ответы** (`want_response = true`) при смене поколения радио

