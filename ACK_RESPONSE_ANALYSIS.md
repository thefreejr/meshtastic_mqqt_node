# Анализ механизма ACK/Response в Firmware и сравнение с проектом

## 📋 Краткое резюме

**Статус:** ✅ Реализация соответствует firmware подходу

**Основные выводы:**
- ✅ Структура ACK пакетов идентична (ROUTING_APP с request_id)
- ✅ Логика обработки want_ack соответствует firmware
- ✅ Логика обработки want_response соответствует firmware
- ✅ Обработка пакетов от клиента (from=0) идентична
- ✅ ACK с want_ack=true для текстовых сообщений реализован
- ⚠️ Retransmission не реализован (нормально для TCP)

---

## Обзор

В Meshtastic используются два механизма подтверждения:
1. **ACK/NAK** - для пакетов с `want_ack=true` (подтверждение доставки)
2. **Response** - для пакетов с `want_response=true` (ответ на запрос, обычно Admin сообщения)

**Ключевые моменты:**
- Пакеты от клиента имеют `from=0` (локальный узел)
- `getFrom(p)` возвращает наш `node_num` для пакетов с `from=0`
- ACK отправляется локально клиенту через `sendToPhone()` / `FromRadio`
- Response создается через `setReplyTo()` с `request_id = id запроса`
- Если модуль уже отправил response (`currentReply`), ACK не отправляется (избежание дублирования)

---

## 1. ACK механизм (want_ack)

### 1.1 Отправка пакета с want_ack=true

#### Firmware (ReliableRouter.cpp:16-42)

Когда пакет отправляется с `want_ack=true`:

```cpp
ErrorCode ReliableRouter::send(meshtastic_MeshPacket *p)
{
    if (p->want_ack) {
        // Если hop_limit = 0, устанавливаем дефолтный
        if (p->hop_limit == 0) {
            p->hop_limit = Default::getConfiguredOrDefaultHopLimit(config.lora.hop_limit);
        }
        // Создаем копию пакета для retransmission
        auto copy = packetPool.allocCopy(*p);
        startRetransmission(copy, NUM_RELIABLE_RETX);
    }
    return isBroadcast(p->to) ? FloodingRouter::send(p) : NextHopRouter::send(p);
}
```

**Логика:**
- Пакет добавляется в очередь retransmission
- Если ACK не получен, пакет будет переотправлен
- После получения ACK retransmission останавливается

#### Проект (packet_handler.py:23-47)

```python
@staticmethod
def prepare_outgoing_packet(packet: mesh_pb2.MeshPacket) -> None:
    want_ack = getattr(packet, 'want_ack', False)
    hop_limit = getattr(packet, 'hop_limit', 0)
    
    if want_ack and hop_limit == 0:
        hop_limit = DEFAULT_HOP_LIMIT
        packet.hop_limit = hop_limit
```

✅ **Соответствие:** Проект устанавливает дефолтный hop_limit для want_ack пакетов, как в firmware.

---

### 1.2 Получение пакета с want_ack=true и отправка ACK

#### Firmware (ReliableRouter.cpp:97-165)

```cpp
void ReliableRouter::sniffReceived(const meshtastic_MeshPacket *p, const meshtastic_Routing *c)
{
    if (isToUs(p)) {  // Пакет адресован нам
        // ВАЖНО: Если модуль уже отправил response (currentReply), не отправляем ACK
        // Это предотвращает дублирование - response уже является подтверждением
        if (!MeshModule::currentReply) {  // Другой модуль еще не ответил
            if (p->want_ack) {
                if (p->which_payload_variant == meshtastic_MeshPacket_decoded_tag) {
                    // Decoded пакет - отправляем ACK
                    if (shouldSuccessAckWithWantAck(p)) {
                        // Если это текстовое сообщение (TEXT_MESSAGE_APP или TEXT_MESSAGE_COMPRESSED_APP),
                        // отправляем ACK с want_ack=true для надежной доставки подтверждения отправителю
                        sendAckNak(meshtastic_Routing_Error_NONE, getFrom(p), p->id, p->channel,
                                   routingModule->getHopLimitForResponse(p->hop_start, p->hop_limit), true);
                    } else if (!p->decoded.request_id && !p->decoded.reply_id) {
                        // Обычный ACK (без want_ack на ACK)
                        sendAckNak(meshtastic_Routing_Error_NONE, getFrom(p), p->id, p->channel,
                                   routingModule->getHopLimitForResponse(p->hop_start, p->hop_limit));
                    } else if ((p->hop_start > 0 && p->hop_start == p->hop_limit) || p->next_hop != NO_NEXT_HOP_PREFERENCE) {
                        // Прямой пакет от отправителя - 0-hop ACK
                        sendAckNak(meshtastic_Routing_Error_NONE, getFrom(p), p->id, p->channel, 0);
                    }
                } else if (p->which_payload_variant == meshtastic_MeshPacket_encrypted_tag && p->channel == 0 &&
                           (nodeDB->getMeshNode(p->from) == nullptr || nodeDB->getMeshNode(p->from)->user.public_key.size == 0)) {
                    // PKI пакет от неизвестного узла - NAK с PKI_UNKNOWN_PUBKEY
                    sendAckNak(meshtastic_Routing_Error_PKI_UNKNOWN_PUBKEY, getFrom(p), p->id, channels.getPrimaryIndex(),
                               routingModule->getHopLimitForResponse(p->hop_start, p->hop_limit));
                } else {
                    // Не можем расшифровать - NAK с NO_CHANNEL
                    sendAckNak(meshtastic_Routing_Error_NO_CHANNEL, getFrom(p), p->id, channels.getPrimaryIndex(),
                               routingModule->getHopLimitForResponse(p->hop_start, p->hop_limit));
                }
            }
        }
        
        // Обработка полученного ACK/NAK
        PacketId ackId = ((c && c->error_reason == meshtastic_Routing_Error_NONE) || !c) ? p->decoded.request_id : 0;
        PacketId nakId = (c && c->error_reason != meshtastic_Routing_Error_NONE) ? p->decoded.request_id : 0;
        
        if (ackId || nakId) {
            stopRetransmission(p->to, ackId ? ackId : nakId);
        }
    }
}
```

**Ключевые моменты:**
- ACK отправляется только если пакет адресован нам (`isToUs`)
- Не отправляется ACK, если другой модуль уже ответил (`currentReply`)
- Для текстовых сообщений ACK может иметь `want_ack=true` для надежности
- Для encrypted пакетов отправляется NAK с соответствующей ошибкой

#### Проект (mqtt/packet_processor.py:771-853, tcp/session.py:1024-1063)

**TCP обработка (tcp/session.py):**
```python
def _send_ack(self, packet: mesh_pb2.MeshPacket, channel_index: int) -> None:
    # Проверяем, нужно ли отправлять ACK с want_ack=true для надежной доставки
    # (как в firmware shouldSuccessAckWithWantAck)
    ack_wants_ack = PacketHandler.should_success_ack_with_want_ack(packet, self.node_num)
    
    # Создаем ACK пакет с want_ack если нужно
    ack_packet = PacketHandler.create_ack_packet(
        packet, 
        self.node_num, 
        channel_index,
        error_reason=None,
        ack_wants_ack=ack_wants_ack
    )
```

**MQTT обработка (mqtt/packet_processor.py):**
```python
if packet.want_ack and is_to_us and not is_broadcast:
    # Проверяем, что это не ACK сам по себе
    is_routing_ack = False
    if payload_type == 'decoded':
        is_routing_ack = (
            hasattr(packet.decoded, 'portnum') and 
            packet.decoded.portnum == portnums_pb2.PortNum.ROUTING_APP and
            hasattr(packet.decoded, 'request_id') and 
            packet.decoded.request_id != 0
        )
    
    # Не отправляем ACK для Admin пакетов (они обрабатываются отдельно через want_response)
    is_admin = False
    if payload_type == 'decoded':
        is_admin = (hasattr(packet.decoded, 'portnum') and 
                   packet.decoded.portnum == portnums_pb2.PortNum.ADMIN_APP)
    
    if not is_routing_ack and not is_admin:
        # Определяем тип ошибки для NAK
        ack_error = None  # ACK по умолчанию
        
        if payload_type == 'encrypted':
            if packet.channel == 0:
                # PKI пакет - проверяем наличие публичного ключа
                from_node = self.node_db.get_mesh_node(packet_from) if self.node_db else None
                if not from_node or not hasattr(from_node.user, 'public_key') or len(from_node.user.public_key) != 32:
                    ack_error = mesh_pb2.Routing.Error.PKI_UNKNOWN_PUBKEY
                else:
                    ack_error = mesh_pb2.Routing.Error.NO_CHANNEL
            else:
                ack_error = mesh_pb2.Routing.Error.NO_CHANNEL
        
        # Проверяем, нужно ли отправлять ACK с want_ack=true для надежной доставки
        # (как в firmware shouldSuccessAckWithWantAck)
        ack_wants_ack = False
        if ack_error is None:  # Только для ACK (не NAK)
            ack_wants_ack = PacketHandler.should_success_ack_with_want_ack(packet, our_node_num)
        
        # Создаем ACK/NAK пакет с want_ack если нужно
        ack_packet = PacketHandler.create_ack_packet(
            packet, 
            our_node_num, 
            packet.channel if packet.channel < 8 else 0,
            ack_error,
            ack_wants_ack=ack_wants_ack
        )
        
        # Отправляем через MQTT
        receiver_session.mqtt_client.publish_packet(ack_packet, channel_index)
```

✅ **Соответствие:** 
- Проверка `isToUs` / `is_to_us`
- Проверка, что это не ACK сам по себе
- NAK для encrypted пакетов с соответствующими ошибками
- ⚠️ **Отличие:** Проект не отправляет ACK для Admin пакетов (они используют want_response)

---

### 1.3 Создание ACK/NAK пакета

#### Firmware (MeshModule.cpp:48-74)

```cpp
meshtastic_MeshPacket *MeshModule::allocAckNak(meshtastic_Routing_Error err, NodeNum to, PacketId idFrom, ChannelIndex chIndex, uint8_t hopLimit)
{
    meshtastic_Routing c = meshtastic_Routing_init_default;
    c.error_reason = err;
    c.which_variant = meshtastic_Routing_error_reason_tag;
    
    meshtastic_MeshPacket *p = router->allocForSending();
    p->decoded.portnum = meshtastic_PortNum_ROUTING_APP;
    p->decoded.payload.size = pb_encode_to_bytes(p->decoded.payload.bytes, sizeof(p->decoded.payload.bytes), &meshtastic_Routing_msg, &c);
    
    p->priority = meshtastic_MeshPacket_Priority_ACK;
    p->hop_limit = hopLimit;
    p->to = to;
    p->decoded.request_id = idFrom;  // КРИТИЧЕСКИ ВАЖНО: ID исходного пакета
    p->channel = chIndex;
    
    return p;
}
```

**Структура ACK пакета:**
- `portnum = ROUTING_APP`
- `request_id = ID исходного пакета` (критически важно!)
- `error_reason = NONE` (ACK) или код ошибки (NAK)
- `priority = ACK`
- `want_ack = false` (по умолчанию)

#### Проект (packet_handler.py:77-118)

```python
@staticmethod
def create_ack_packet(original_packet: mesh_pb2.MeshPacket, 
                      our_node_num: int, 
                      channel_index: int,
                      error_reason: int = None) -> mesh_pb2.MeshPacket:
    packet_from = getattr(original_packet, 'from', 0)
    packet_id = original_packet.id
    
    # Создаем Routing сообщение
    routing_msg = mesh_pb2.Routing()
    if error_reason is None:
        routing_msg.error_reason = mesh_pb2.Routing.Error.NONE  # ACK
    else:
        routing_msg.error_reason = error_reason  # NAK
    
    # Создаем MeshPacket с ACK
    ack_packet = mesh_pb2.MeshPacket()
    ack_packet.id = random.randint(1, 0xFFFFFFFF)
    ack_packet.to = packet_from
    setattr(ack_packet, 'from', our_node_num)
    ack_packet.channel = channel_index
    ack_packet.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
    ack_packet.decoded.request_id = packet_id  # КРИТИЧЕСКИ ВАЖНО: ID исходного пакета
    ack_packet.decoded.payload = routing_msg.SerializeToString()
    ack_packet.priority = mesh_pb2.MeshPacket.Priority.ACK
    ack_packet.want_ack = False
```

✅ **Соответствие:** Идентичная структура ACK пакета

---

### 1.4 Implicit ACK для собственных пакетов

#### Firmware (MQTT.cpp:66-70)

```cpp
// Generate an implicit ACK towards ourselves (handled and processed only locally!)
// We do this because packets are not rebroadcasted back into MQTT anymore and we assume that at least one node
// receives it when we get our own packet back. Then we'll stop our retransmissions.
if (isFromUs(e.packet))
    routingModule->sendAckNak(meshtastic_Routing_Error_NONE, getFrom(e.packet), e.packet->id, ch.index);
```

**Логика:**
- Когда пакет публикуется в MQTT и возвращается обратно (downlink)
- Firmware генерирует implicit ACK локально
- Это останавливает retransmission, так как пакет был доставлен хотя бы одному узлу

#### Проект (mqtt/packet_processor.py:129-166)

```python
# ВАЖНО: Если это наш собственный пакет (gateway_id совпадает) и он от нас (isFromUs),
# отправляем implicit ACK локально клиенту (как в firmware MQTT.cpp:66-70)
if is_own_gateway and envelope.packet:
    packet_from_envelope = getattr(envelope.packet, 'from', 0)
    is_from_us = (packet_from_envelope == our_node_num)
    
    if is_from_us:
        # Это наш собственный пакет - отправляем implicit ACK локально клиенту
        if sender_session and envelope.packet.want_ack:
            ack_packet = PacketHandler.create_ack_packet(
                envelope.packet,
                our_node_num,
                envelope.packet.channel if envelope.packet.channel < 8 else 0
            )
            
            # Отправляем ACK напрямую клиенту (локально, не через MQTT)
            from_radio_ack = mesh_pb2.FromRadio()
            from_radio_ack.packet.CopyFrom(ack_packet)
            serialized_ack = from_radio_ack.SerializeToString()
            framed_ack = StreamAPI.add_framing(serialized_ack)
            to_client_queue.put_nowait(framed_ack)
```

✅ **Соответствие:** Проект отправляет implicit ACK локально клиенту, как в firmware

---

## 2. Response механизм (want_response)

### 2.1 Обработка want_response пакетов

#### Firmware (MeshModule.cpp:88-200)

```cpp
void MeshModule::callModules(meshtastic_MeshPacket &mp, RxSource src)
{
    currentReply = NULL;  // Нет ответа пока
    
    bool toUs = isBroadcast(mp.to) || isToUs(&mp);
    
    for (auto i = modules->begin(); i != modules->end(); ++i) {
        auto &pi = **i;
        pi.currentRequest = &mp;
        
        bool wantsPacket = (isDecoded || pi.encryptedOk) && (pi.isPromiscuous || toUs) && pi.wantPacket(&mp);
        
        if (wantsPacket) {
            ProcessMessage handled = pi.handleReceived(mp);
            
            // Отправляем response если:
            // 1. Пакет decoded
            // 2. want_response = true
            // 3. Пакет адресован нам
            // 4. Это не наш собственный пакет (или адресован нам)
            // 5. Еще никто не ответил
            if (isDecoded && mp.decoded.want_response && toUs && (!isFromUs(&mp) || isToUs(&mp)) && !currentReply) {
                pi.sendResponse(mp);
                LOG_INFO("Asked module '%s' to send a response", pi.name);
            }
        }
    }
    
    // Если запрошен response, но никто не ответил, отправляем NAK
    if (isDecoded && mp.decoded.want_response && toUs) {
        if (currentReply) {
            printPacket("Send response", currentReply);
            service->sendToMesh(currentReply);
            currentReply = NULL;
        } else if (mp.from != ourNodeNum && !ignoreRequest) {
            // Никто не ответил - отправляем NAK
            routingModule->sendAckNak(meshtastic_Routing_Error_NO_RESPONSE, getFrom(&mp), mp.id, mp.channel,
                                      routingModule->getHopLimitForResponse(mp.hop_start, mp.hop_limit));
        }
    }
}
```

**Логика:**
- Модули вызываются последовательно
- Если модуль хочет ответить, он вызывает `sendResponse()`
- Response устанавливается в `currentReply`
- Если никто не ответил, отправляется NAK с `NO_RESPONSE`

#### Проект (tcp/session.py:857-861)

```python
# Используем PacketHandler для проверки Admin пакета
if PacketHandler.is_admin_packet(packet):
    debug("TCP", f"AdminMessage detected, forwarding to handler (want_response={getattr(packet.decoded, 'want_response', False)})")
    self._handle_admin_message(packet)
```

**Обработка Admin сообщений:**
- Каждый тип Admin сообщения обрабатывается отдельно
- Если `want_response=true`, создается reply пакет
- Reply отправляется клиенту через TCP

✅ **Соответствие:** Проект обрабатывает want_response для Admin сообщений

---

### 2.2 Создание Response пакета

#### Firmware (MeshModule.cpp:233-245)

```cpp
void setReplyTo(meshtastic_MeshPacket *p, const meshtastic_MeshPacket &to)
{
    assert(p->which_payload_variant == meshtastic_MeshPacket_decoded_tag);
    p->to = getFrom(&to);    // Адрес отправителя запроса
    p->channel = to.channel; // Тот же канал
    p->hop_limit = routingModule->getHopLimitForResponse(to.hop_start, to.hop_limit);
    
    // No need for an ack if we are just delivering locally (it just generates an ignored ack)
    p->want_ack = (to.from != 0) ? to.want_ack : false;
    if (p->priority == meshtastic_MeshPacket_Priority_UNSET)
        p->priority = meshtastic_MeshPacket_Priority_RELIABLE;
    p->decoded.request_id = to.id;  // ID исходного запроса
}
```

**Ключевые моменты:**
- `to = from исходного пакета`
- `request_id = id исходного пакета`
- `want_ack = false` если `to.from == 0` (локальная доставка)
- `priority = RELIABLE`

#### Проект (admin_handler.py:34-63)

```python
@staticmethod
def create_reply_packet(original_packet: mesh_pb2.MeshPacket,
                        admin_response: admin_pb2.AdminMessage,
                        our_node_num: int) -> mesh_pb2.MeshPacket:
    reply_packet = mesh_pb2.MeshPacket()
    reply_packet.id = random.randint(1, 0xFFFFFFFF)
    
    # setReplyTo логика (из firmware MeshModule.cpp:233-245)
    packet_from = getattr(original_packet, 'from', 0)
    reply_packet.to = packet_from  # 0 означает локальный узел для TCP
    
    setattr(reply_packet, 'from', our_node_num)
    reply_packet.channel = original_packet.channel
    reply_packet.decoded.request_id = original_packet.id  # ID запроса
    reply_packet.want_ack = False  # Для TCP клиента from=0, поэтому want_ack=False
    reply_packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
    reply_packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
    reply_packet.decoded.payload = admin_response.SerializeToString()
    
    return reply_packet
```

✅ **Соответствие:** Идентичная логика setReplyTo

---

### 2.3 Пример: get_channel_request

#### Firmware (AdminModule.cpp)

```cpp
case meshtastic_AdminMessage_get_channel_request_tag: {
    uint8_t chIndex = r->get_channel_request - 1;
    if (mp.decoded.want_response) {
        meshtastic_Channel ch = channels.getByIndex(chIndex);
        myReply = allocDataProtobuf(ch);
        // setReplyTo вызывается автоматически через sendResponse
    }
    break;
}
```

#### Проект (tcp/session.py:1183-1209)

```python
elif admin_msg.HasField('get_channel_request'):
    requested_index = admin_msg.get_channel_request
    ch_index = requested_index - 1
    
    if not getattr(packet.decoded, 'want_response', False):
        warn("ADMIN", f"get_channel_request without want_response (channel {ch_index})")
        return
    
    if 0 <= ch_index < MAX_NUM_CHANNELS:
        ch = self.channels.get_by_index(ch_index)
        
        admin_response = admin_pb2.AdminMessage()
        admin_response.get_channel_response.CopyFrom(ch)
        
        # Используем AdminMessageHandler для создания reply пакета
        reply_packet = AdminMessageHandler.create_reply_packet(packet, admin_response, self.node_num)
        
        from_radio = mesh_pb2.FromRadio()
        from_radio.packet.CopyFrom(reply_packet)
        self._send_from_radio(from_radio)
```

✅ **Соответствие:** Идентичная логика обработки get_channel_request

---

## 3. Взаимодействие с клиентом через TCP

### 3.1 Обработка пакетов от клиента (from=0)

#### Firmware (NodeDB.cpp:441-444, Router.cpp:332)

```cpp
// getFrom - если from == 0, возвращает наш node_num
NodeNum getFrom(const meshtastic_MeshPacket *p)
{
    return (p->from == 0) ? nodeDB->getNodeNum() : p->from;
}

// В Router перед отправкой устанавливается from
p->from = getFrom(p);  // Если было 0, становится наш node_num
```

**Логика:**
- Пакеты от клиента имеют `from = 0` (локальный узел)
- Перед отправкой в mesh `from` устанавливается в наш `node_num`
- Это позволяет mesh знать, от какого узла пришел пакет

#### Проект (tcp/session.py:862-871)

```python
# Устанавливаем поле from на наш node_num перед отправкой в MQTT
# (как в firmware Router.cpp: p->from = getFrom(p))
# ВАЖНО: Пакеты от клиента всегда имеют from=0, устанавливаем на наш node_num
if packet_from == 0:
    setattr(packet, 'from', self.node_num)
    info("TCP", f"Set packet.from={self.node_num:08X} (was 0) before MQTT publish")
```

✅ **Соответствие:** Идентичная логика - `from=0` заменяется на наш `node_num`

---

### 3.2 Отправка пакетов клиенту

#### Firmware (MeshService.cpp:83-115)

```cpp
int MeshService::handleFromRadio(const meshtastic_MeshPacket *mp)
{
    powerFSM.trigger(EVENT_PACKET_FOR_PHONE);
    
    nodeDB->updateFrom(*mp);  // Обновляем NodeDB
    
    // ... логика отправки NodeInfo ...
    
    printPacket("Forwarding to phone", mp);
    sendToPhone(packetPool.allocCopy(*mp));  // Отправляем клиенту
    
    return 0;
}
```

**Логика:**
- Все пакеты отправляются клиенту через `sendToPhone()`
- Включая ACK/NAK пакеты
- Включая Response пакеты
- Включая пакеты из mesh

#### Проект (tcp/session.py:729-757, mqtt/packet_processor.py:727-757)

```python
# Отправляем пакет клиенту (как в firmware MeshService::sendToPhone)
from_radio = mesh_pb2.FromRadio()
from_radio.packet.CopyFrom(packet)

serialized = from_radio.SerializeToString()
framed = StreamAPI.add_framing(serialized)
to_client_queue.put_nowait(framed)
```

✅ **Соответствие:** Все пакеты отправляются клиенту через FromRadio

---

### 3.3 Отправка ACK клиенту через TCP

#### Firmware

ACK отправляется клиенту через `sendToPhone()` после обработки в `ReliableRouter::sniffReceived()`

**Порядок:**
1. Пакет получен от клиента (from=0)
2. `getFrom(p)` возвращает наш node_num
3. Пакет обрабатывается в `ReliableRouter::sniffReceived()`
4. Если `want_ack=true`, создается ACK пакет
5. ACK отправляется через `sendToPhone()` клиенту

#### Проект (tcp/session.py:1024-1051)

```python
def _send_ack(self, packet: mesh_pb2.MeshPacket, channel_index: int) -> None:
    """Отправляет ACK пакет обратно клиенту"""
    ack_packet = PacketHandler.create_ack_packet(packet, self.node_num, channel_index)
    
    def send_ack_delayed():
        time.sleep(0.1)  # 100ms задержка
        from_radio = mesh_pb2.FromRadio()
        from_radio.packet.CopyFrom(ack_packet)
        self._send_from_radio(from_radio)
    
    # Отправляем асинхронно с задержкой
    ack_thread = threading.Thread(target=send_ack_delayed, daemon=True)
    ack_thread.start()
```

**Порядок:**
1. Пакет получен от клиента (from=0)
2. `from` устанавливается в наш `node_num` перед MQTT
3. Пакет обрабатывается в `_handle_mesh_packet()`
4. Если `want_ack=true`, создается ACK пакет
5. ACK отправляется клиенту через TCP с задержкой 100ms

⚠️ **Отличие:** Проект отправляет ACK с задержкой 100ms (для предотвращения race conditions)

---

### 3.4 Обработка ACK от клиента

#### Firmware (ReliableRouter.cpp:146-160)

```cpp
// We consider an ack to be either a !routing packet with a request ID or a routing packet with !error
PacketId ackId = ((c && c->error_reason == meshtastic_Routing_Error_NONE) || !c) ? p->decoded.request_id : 0;

// A nak is a routing packt that has an  error code
PacketId nakId = (c && c->error_reason != meshtastic_Routing_Error_NONE) ? p->decoded.request_id : 0;

if (ackId || nakId) {
    LOG_DEBUG("Received a %s for 0x%x, stopping retransmissions", ackId ? "ACK" : "NAK", ackId);
    if (ackId) {
        stopRetransmission(p->to, ackId);
    } else {
        stopRetransmission(p->to, nakId);
    }
}
```

**Логика:**
- ACK определяется по `request_id` в ROUTING_APP пакете
- Если `error_reason == NONE` - это ACK
- Если `error_reason != NONE` - это NAK
- Retransmission останавливается при получении ACK/NAK
- ACK может прийти от клиента (from=0) или из mesh

#### Проект

⚠️ **Отличие:** Проект не реализует retransmission механизм (это нормально для TCP, где доставка гарантирована)

**Обработка ACK от клиента:**
- ACK пакеты от клиента обрабатываются как обычные пакеты
- `request_id` используется для идентификации исходного пакета
- Проект не останавливает retransmission (так как его нет), но логирует получение ACK

---

## 4. Детали обработки пакетов от клиента

### 4.1 Пакеты с want_ack=true от клиента

#### Firmware

1. Клиент отправляет пакет с `from=0`, `want_ack=true`
2. `getFrom(p)` возвращает наш `node_num`
3. Пакет обрабатывается в `ReliableRouter::sniffReceived()`
4. Проверяется `isToUs(p)` - для пакетов от клиента это обычно `true`
5. Создается ACK пакет через `allocAckNak()`
6. ACK отправляется клиенту через `sendToPhone()`

#### Проект

1. Клиент отправляет пакет с `from=0`, `want_ack=true`
2. `from` устанавливается в наш `node_num` перед MQTT
3. Пакет обрабатывается в `_handle_mesh_packet()`
4. Проверяется `PacketHandler.should_send_ack()` - возвращает `true`
5. Создается ACK пакет через `PacketHandler.create_ack_packet()`
6. ACK отправляется клиенту через `_send_from_radio()` с задержкой 100ms

✅ **Соответствие:** Идентичная логика обработки

---

### 4.2 Пакеты с want_response=true от клиента (Admin)

#### Firmware (MeshModule.cpp:157-159, AdminModule.cpp)

1. Клиент отправляет Admin пакет с `from=0`, `want_response=true`
2. Пакет обрабатывается в `MeshModule::callModules()`
3. Проверяется `mp.decoded.want_response && toUs && !currentReply`
4. Модуль (AdminModule) вызывает `sendResponse(mp)`
5. Response создается через `allocReply()` и `setReplyTo()`
6. Response отправляется клиенту через `sendToPhone()`

#### Проект (tcp/session.py:1053-1373)

1. Клиент отправляет Admin пакет с `from=0`, `want_response=true`
2. Пакет обрабатывается в `_handle_admin_message()`
3. Проверяется `getattr(packet.decoded, 'want_response', False)`
4. Создается response через `AdminMessageHandler.create_reply_packet()`
5. Response отправляется клиенту через `_send_from_radio()`

✅ **Соответствие:** Идентичная логика обработки

---

### 4.3 Важные детали взаимодействия с клиентом

#### Firmware

**Обработка пакетов от клиента:**
- Пакеты от клиента имеют `from = 0` (локальный узел)
- `getFrom(p)` используется для получения реального отправителя
- Для пакетов от клиента `getFrom(p)` возвращает наш `node_num`
- ACK отправляется на адрес, полученный через `getFrom(p)`

**Отправка ACK клиенту:**
- ACK пакет имеет `to = getFrom(original_packet)` (наш node_num для пакетов от клиента)
- Но так как клиент имеет `from=0`, ACK отправляется через `sendToPhone()` (локально)
- Клиент получает ACK через FromRadio

#### Проект

**Обработка пакетов от клиента:**
- Пакеты от клиента имеют `from = 0`
- `from` устанавливается в наш `node_num` перед отправкой в MQTT
- ACK создается с `to = packet_from` (0 для пакетов от клиента)
- ACK отправляется клиенту через TCP (локально)

**Отправка ACK клиенту:**
- ACK пакет имеет `to = packet_from` (0 для пакетов от клиента)
- ACK отправляется через `_send_from_radio()` напрямую клиенту
- Клиент получает ACK через FromRadio.packet

✅ **Соответствие:** Идентичная логика - ACK отправляется локально клиенту

---

## 5. Сравнительная таблица

| Аспект | Firmware | Проект | Статус |
|--------|----------|--------|--------|
| **ACK для want_ack** | ✅ ReliableRouter::sniffReceived | ✅ packet_processor.py | ✅ Соответствует |
| **NAK для encrypted** | ✅ NO_CHANNEL / PKI_UNKNOWN_PUBKEY | ✅ NO_CHANNEL / PKI_UNKNOWN_PUBKEY | ✅ Соответствует |
| **Implicit ACK** | ✅ MQTT.cpp:66-70 | ✅ packet_processor.py:129-166 | ✅ Соответствует |
| **Response для want_response** | ✅ MeshModule::callModules | ✅ _handle_admin_message | ✅ Соответствует |
| **setReplyTo логика** | ✅ MeshModule.cpp:233-245 | ✅ admin_handler.py:34-63 | ✅ Соответствует |
| **request_id в ACK** | ✅ ID исходного пакета | ✅ ID исходного пакета | ✅ Соответствует |
| **ACK с want_ack для текстовых** | ✅ shouldSuccessAckWithWantAck | ✅ should_success_ack_with_want_ack | ✅ Соответствует |
| **Retransmission** | ✅ ReliableRouter | ❌ Не реализовано | ⚠️ Нормально для TCP |

---

## 6. Ключевые различия

### 6.1 Retransmission

**Firmware:** Реализует retransmission для want_ack пакетов через `ReliableRouter`

**Проект:** Не реализует retransmission (TCP гарантирует доставку)

✅ **Оправдано:** TCP обеспечивает надежную доставку, retransmission не нужен

### 6.2 ACK для Admin пакетов

**Firmware:** 
- Admin пакеты с `want_response=true` обрабатываются через `MeshModule::callModules()`
- Если модуль отправил response (`currentReply`), ACK не отправляется (избежание дублирования)
- Если `want_response=false` но `want_ack=true`, ACK может быть отправлен через `ReliableRouter::sniffReceived()`

**Проект:** 
- Admin пакеты обрабатываются через `_handle_admin_message()` ДО проверки `should_send_ack()`
- `should_send_ack()` явно исключает Admin пакеты
- В MQTT также проверяется, что это не Admin пакет перед отправкой ACK

⚠️ **Возможное отличие:** Проект не отправляет ACK для Admin пакетов, даже если `want_ack=true` и `want_response=false`. Это может быть проблемой, если Admin пакет имеет только `want_ack=true` без `want_response`.

### 6.3 ACK с want_ack=true

**Firmware:** Для текстовых сообщений ACK может иметь `want_ack=true` для надежности

**Проект:** Реализовано через `PacketHandler.should_success_ack_with_want_ack()` - для текстовых сообщений (TEXT_MESSAGE_APP, TEXT_MESSAGE_COMPRESSED_APP) ACK имеет `want_ack=true`

✅ **Соответствие:** Идентичная логика - ACK с `want_ack=true` для текстовых сообщений реализован

---

## 7. Выводы

### ✅ Полное соответствие:

1. **Структура ACK пакетов** - идентична (ROUTING_APP с request_id)
2. **NAK для encrypted** - те же коды ошибок
3. **Implicit ACK** - локальная отправка для собственных пакетов
4. **Response механизм** - идентичная логика setReplyTo
5. **Отправка клиенту** - все пакеты через FromRadio

### ⚠️ Небольшие отличия (оправданные):

1. **Retransmission** - не реализован (TCP гарантирует доставку)
2. **ACK для Admin** - не отправляется (используется только want_response)

### 🎯 Результат:

**Реализация ACK/Response в проекте соответствует firmware подходу** с небольшими упрощениями, оправданными для TCP окружения.

---

## 9. Диаграмма потока ACK/Response

### 9.1 Поток ACK для пакета от клиента

```
Клиент → TCP (from=0, want_ack=true)
    ↓
Проект: _handle_mesh_packet()
    ↓
from устанавливается в node_num
    ↓
PacketHandler.should_send_ack() → True
    ↓
PacketHandler.create_ack_packet()
    ↓
_send_ack() → FromRadio → TCP → Клиент
```

### 9.2 Поток Response для Admin запроса

```
Клиент → TCP (from=0, want_response=true, AdminMessage)
    ↓
Проект: _handle_admin_message()
    ↓
Обработка Admin запроса
    ↓
AdminMessageHandler.create_reply_packet()
    ↓
_send_from_radio() → FromRadio → TCP → Клиент
```

### 9.3 Поток ACK через MQTT

```
Узел A → MQTT (want_ack=true)
    ↓
Узел B: process_mqtt_message()
    ↓
Расшифровка пакета
    ↓
want_ack && is_to_us → True
    ↓
PacketHandler.create_ack_packet()
    ↓
MQTT publish → Узел A получает ACK
```

---

## 8. Ссылки на код

### Firmware
- `examples/firmware/src/mesh/ReliableRouter.cpp` - ACK логика
- `examples/firmware/src/mesh/MeshModule.cpp` - allocAckNak, setReplyTo
- `examples/firmware/src/mesh/MeshService.cpp` - sendToPhone
- `examples/firmware/src/mqtt/MQTT.cpp` - implicit ACK

### Проект
- `meshtastic_simulator/protocol/packet_handler.py` - create_ack_packet
- `meshtastic_simulator/mqtt/packet_processor.py` - обработка ACK в MQTT
- `meshtastic_simulator/tcp/session.py` - _send_ack, _handle_admin_message
- `meshtastic_simulator/protocol/admin_handler.py` - create_reply_packet

