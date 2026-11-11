FROM python:3.10.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "python migrate_config_settings.py && python migrate_post_status.py && python migrate_multiserver_support.py && python bot.py"]