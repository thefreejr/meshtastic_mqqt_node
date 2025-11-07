FROM python:3.11-slim

WORKDIR /app

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Копируем файлы зависимостей
COPY requirements_meshtastic_node.txt .

# Устанавливаем Python зависимости
RUN pip install --no-cache-dir -r requirements_meshtastic_node.txt

# Копируем код приложения
COPY meshtastic_simulator/ ./meshtastic_simulator/
COPY meshtastic_mqtt_node.py .
COPY config/ ./config/

# Создаем директории для логов и данных
RUN mkdir -p /app/logs /app/data

# Устанавливаем переменные окружения
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Открываем порт TCP
EXPOSE 4403

# Запускаем приложение
CMD ["python", "meshtastic_mqtt_node.py"]


