FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

WORKDIR /app

# Instalar dependências python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Quebrar cache
COPY version.txt .
# Copia os arquivos da API e do Bot
COPY roboblaze_api/ ./roboblaze_api/
COPY roboblaze_scraper/ ./roboblaze_scraper/
COPY blaze.py .
COPY fetch_90k.py .
COPY fetch_45k_brancos.py .
COPY fetch_next_120k.py .
COPY fetch_5k.py .
COPY fetch_120_days.py .

# Comando padrão será rodar a API (o worker será sobrescrito no docker-compose)
CMD ["uvicorn", "roboblaze_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
