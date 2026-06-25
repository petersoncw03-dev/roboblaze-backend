FROM python:3.10-slim

WORKDIR /app

# Instalar dependências essenciais do sistema
RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Quebrar cache
COPY version.txt .
# Copia os arquivos da API e do Bot
COPY roboblaze_api/ ./roboblaze_api/
COPY blaze.py .

# Comando padrão será rodar a API (o worker será sobrescrito no docker-compose)
CMD ["uvicorn", "roboblaze_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "12"]
