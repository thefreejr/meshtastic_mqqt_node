"""
API endpoints для управления ботами
"""

from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1/bots", tags=["bots"])


# Pydantic модели для валидации
class BotCreateRequest(BaseModel):
    bot_id: str = Field(..., description="Уникальный ID бота")
    config: Dict[str, Any] = Field(..., description="Конфигурация бота")


class BotSendMessageRequest(BaseModel):
    message: str = Field(..., description="Текст сообщения")
    destination: Optional[str] = Field(None, description="Node ID получателя (None = broadcast)")
    channel: int = Field(0, description="Индекс канала")
    want_ack: bool = Field(False, description="Запрос подтверждения доставки")


class BotResponse(BaseModel):
    success: bool
    bot_id: Optional[str] = None
    node_id: Optional[str] = None
    message: str


class BotMessageResponse(BaseModel):
    success: bool
    packet_id: Optional[int] = None
    bot_id: str
    message: str
    sent_at: str


# Глобальная переменная для BotManager (будет установлена при инициализации API)
bot_manager = None


def set_bot_manager(manager):
    """Устанавливает BotManager для использования в endpoints"""
    global bot_manager
    bot_manager = manager


@router.get("/statistics", summary="Статистика всех ботов")
async def get_bots_statistics():
    """
    Возвращает статистику всех ботов
    """
    if bot_manager is None:
        raise HTTPException(status_code=503, detail="Bot manager not initialized")
    
    try:
        stats = bot_manager.get_statistics()
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting statistics: {str(e)}")


@router.post("", summary="Создать бота", response_model=BotResponse)
async def create_bot(request: BotCreateRequest):
    """
    Создает нового бота
    """
    if bot_manager is None:
        raise HTTPException(status_code=503, detail="Bot manager not initialized")
    
    try:
        bot = bot_manager.create_bot(request.bot_id, request.config)
        if bot:
            return BotResponse(
                success=True,
                bot_id=bot.bot_id,
                node_id=bot.node_id,
                message="Bot created successfully"
            )
        else:
            raise HTTPException(status_code=400, detail="Failed to create bot")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating bot: {str(e)}")


@router.delete("/{bot_id}", summary="Удалить бота", response_model=BotResponse)
async def delete_bot(bot_id: str):
    """
    Удаляет бота
    """
    if bot_manager is None:
        raise HTTPException(status_code=503, detail="Bot manager not initialized")
    
    try:
        if bot_manager.delete_bot(bot_id):
            return BotResponse(
                success=True,
                message="Bot deleted successfully"
            )
        else:
            raise HTTPException(status_code=404, detail="Bot not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting bot: {str(e)}")


@router.post("/{bot_id}/messages/send", summary="Отправить сообщение от имени бота", response_model=BotMessageResponse)
async def send_bot_message(bot_id: str, request: BotSendMessageRequest):
    """
    Отправляет сообщение от имени бота
    """
    if bot_manager is None:
        raise HTTPException(status_code=503, detail="Bot manager not initialized")
    
    try:
        bot = bot_manager.get_bot(bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        
        packet_id = bot.send_message(
            text=request.message,
            destination=request.destination,
            channel=request.channel,
            want_ack=request.want_ack
        )
        
        if packet_id:
            return BotMessageResponse(
                success=True,
                packet_id=packet_id,
                bot_id=bot_id,
                message=request.message,
                sent_at=datetime.now().isoformat()
            )
        else:
            raise HTTPException(status_code=500, detail="Failed to send message")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error sending message: {str(e)}")


@router.get("/{bot_id}/messages", summary="Получить сообщения бота")
async def get_bot_messages(
    bot_id: str,
    limit: int = Query(50, ge=1, le=1000, description="Количество сообщений"),
    offset: int = Query(0, ge=0, description="Смещение для пагинации"),
    since: Optional[str] = Query(None, description="ISO timestamp, только сообщения после этой даты")
):
    """
    Получает список сообщений, полученных ботом
    """
    if bot_manager is None:
        raise HTTPException(status_code=503, detail="Bot manager not initialized")
    
    try:
        bot = bot_manager.get_bot(bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        
        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace('Z', '+00:00'))
            except:
                raise HTTPException(status_code=400, detail="Invalid since format, use ISO 8601")
        
        messages_data = bot.get_messages(limit=limit, offset=offset, since=since_dt)
        
        return {
            'bot_id': bot_id,
            **messages_data
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting messages: {str(e)}")


