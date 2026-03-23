FROM python:3.12-slim

LABEL maintainer="github-word-miner"
LABEL description="Extrae palabras de nombres de funciones desde repos Python y Java de GitHub"

WORKDIR /app

# Instalar dependencias primero (aprovecha la caché de capas)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY miner.py .

CMD ["python", "-u", "miner.py"]