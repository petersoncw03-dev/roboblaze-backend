import asyncio
import httpx
from datetime import datetime, timezone
import logging
from sqlalchemy.dialects.postgresql import insert
import os
from dotenv import load_dotenv

# Importa as configurações do banco de dados do main/database
from database import AsyncSessionLocal, Result

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Fetch10k")

# Novo endpoint descoberto na blaze.bet.br
HISTORY_API_URL = "https://blaze.bet.br/api/singleplayer-originals/originals/roulette_games/recent/history/1"

def format_color(color_int: int) -> str:
    if color_int == 0: return "BRANCO"
    elif color_int == 1: return "VERMELHO"
    elif color_int == 2: return "PRETO"
    return "UNKNOWN"

async def save_results_batch(results: list):
    if not results: return
    async with AsyncSessionLocal() as session:
        stmt = insert(Result).values(results)
        stmt = stmt.on_conflict_do_nothing(index_elements=['id'])
        await session.execute(stmt)
        await session.commit()

async def fetch_page(client, page, end_date_str):
    url = f"{HISTORY_API_URL}?endDate={end_date_str}&page={page}"
    try:
        response = await client.get(url)
        if response.status_code == 200:
            data = response.json()
            return data.get("records", [])
        else:
            logger.error(f"Erro na pág {page}: Status {response.status_code}")
    except Exception as e:
        logger.error(f"Exceção na pág {page}: {e}")
    return []

async def fetch_10k_history():
    logger.info("🚀 Iniciando o resgate de 10.000 pedras recentes (aprox 7 dias)...")
    
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    total_saved = 0
    pages_to_fetch = 100 # 100 páginas * 100 registros = 10.000 pedras
    
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        # Pega de trás para frente para salvar na ordem cronológica (opcional, on_conflict_do_nothing resolve)
        # Vamos pegar da pág 1 até 300
        for page in range(1, pages_to_fetch + 1):
            logger.info(f"Baixando página {page}/{pages_to_fetch}...")
            records = await fetch_page(client, page, now_iso)
            
            if not records:
                logger.warning(f"Página {page} veio vazia. Parando busca.")
                break
                
            db_records = []
            for item in records:
                try:
                    ts_str = item.get("created_at")
                    dt = datetime.strptime(ts_str.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
                except:
                    dt = datetime.now(timezone.utc)
                    
                db_records.append({
                    "id": str(item["id"]),
                    "color": format_color(item["color"]),
                    "roll": item["roll"],
                    "timestamp": dt,
                    "total_bets": 0.0,
                    "total_payout": 0.0,
                    "house_profit": 0.0
                })
                
            await save_results_batch(db_records)
            total_saved += len(db_records)
            logger.info(f"✅ Página {page} salva. Total até agora: {total_saved} pedras.")
            
            # Pequeno delay para não sobrecarregar a Blaze
            await asyncio.sleep(1)

    logger.info(f"🎉 Resgate de 10k concluído! {total_saved} pedras salvas no banco.")

if __name__ == "__main__":
    asyncio.run(fetch_10k_history())
