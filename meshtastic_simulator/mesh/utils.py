"""
Утилиты для mesh (MAC адрес, Node ID)
"""

import uuid
import random
from typing import Optional


def get_mac_address() -> bytes:
    """Получает MAC адрес сетевой карты (как getMacAddr в firmware)"""
    # Получаем MAC адрес первой сетевой карты
    mac = uuid.getnode()
    # Преобразуем в байты (6 байт)
    mac_bytes = mac.to_bytes(6, byteorder='big')
    return mac_bytes


def generate_node_id() -> str:
    """Генерирует Node ID на основе MAC адреса (как NodeDB::pickNewNodeNum и getNodeId)"""
    try:
        # Получаем MAC адрес
        mac_addr = get_mac_address()
        
        # Генерируем nodeNum из последних 4 байт MAC адреса (как в firmware: pickNewNodeNum)
        # nodeNum = (ourMacAddr[2] << 24) | (ourMacAddr[3] << 16) | (ourMacAddr[4] << 8) | ourMacAddr[5];
        node_num = (mac_addr[2] << 24) | (mac_addr[3] << 16) | (mac_addr[4] << 8) | mac_addr[5]
        
        # Формируем Node ID (как getNodeId: "!%08x")
        node_id = f"!{node_num:08X}"
        
        print(f"📡 MAC адрес: {':'.join(f'{b:02X}' for b in mac_addr)}")
        print(f"📡 Node ID (на основе MAC): {node_id}")
        
        return node_id
    except Exception as e:
        print(f"⚠ Ошибка получения MAC адреса: {e}, используем случайный ID")
        # Fallback на случайный ID если не удалось получить MAC
        node_num = random.randint(0, 0xFFFFFFFF)
        return f"!{node_num:08X}"

