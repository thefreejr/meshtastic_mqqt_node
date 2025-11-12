"""
API сервер для управления симулятором
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
from ..utils.logger import info, error
from .routes import bots


class APIServer:
    """API сервер для управления симулятором"""
    
    def __init__(self, bot_manager=None, host: str = "0.0.0.0", port: int = 8080):
        """
        Инициализирует API сервер
        
        Args:
            bot_manager: Экземпляр BotManager
            host: Хост для прослушивания
            port: Порт для прослушивания
        """
        self.app = FastAPI(
            title="Meshtastic Node Simulator API",
            description="API для управления симулятором Meshtastic MQTT Node",
            version="1.0.0"
        )
        
        # CORS middleware
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        
        # Устанавливаем BotManager для routes
        if bot_manager:
            bots.set_bot_manager(bot_manager)
        
        # Регистрируем routes
        self.app.include_router(bots.router)
        
        # Health check endpoint
        @self.app.get("/health")
        async def health():
            return {"status": "ok"}
        
        self.host = host
        self.port = port
        self.server = None
    
    def start(self):
        """Запускает API сервер"""
        try:
            import uvicorn
            info("API", f"Starting API server on {self.host}:{self.port}")
            uvicorn.run(self.app, host=self.host, port=self.port, log_level="info")
        except ImportError:
            error("API", "uvicorn not installed. Install with: pip install uvicorn[standard]")
        except Exception as e:
            error("API", f"Error starting API server: {e}")


