import asyncio
import httpx
from datetime import datetime, timezone, timedelta
import logging
from sqlalchemy.dialects.postgresql import insert
import os

from roboblaze_scraper.database import AsyncSessionLocal, Result
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Fetch120d")

MIRRORS = ["blaze.bet.br"]
current_mirror_idx = 0

def format_color(color_int: int) -> str:
    if color_int == 0: return "BRANCO"
    elif color_int == 1: return "VERMELHO"
    elif color_int == 2: return "PRETO"
    return "UNKNOWN"

async def save_results_batch(results: list):
    if not results: return
    async with AsyncSessionLocal() as session:
        stmt = insert(Result).values(results)
        # O banco cuidará de ignorar as pedras que já existirem (por id)
        stmt = stmt.on_conflict_do_nothing(index_elements=['id'])
        await session.execute(stmt)
        await session.commit()

async def fetch_page_with_mirror_rotation(client, page, end_date_str, attempt=1):
    global current_mirror_idx
    domain = MIRRORS[current_mirror_idx]
    url = f"https://{domain}/api/singleplayer-originals/originals/roulette_games/recent/history/1?endDate={end_date_str}&page={page}"
    
    try:
        response = await client.get(url)
        if response.status_code == 429:
            if attempt <= 15:
                delay = 3.0 * attempt
                logger.warning(f"⚠️ 429 na pág {page}. Esperando {delay}s...")
                await asyncio.sleep(delay)
                return await fetch_page_with_mirror_rotation(client, page, end_date_str, attempt + 1)
            else:
                raise Exception(f"HTTP Status 429 após 15 tentativas.")
                
        if response.status_code != 200:
            if attempt <= 10:
                logger.warning(f"⚠️ HTTP {response.status_code} na pág {page}. Tentando novamente em 3s...")
                await asyncio.sleep(3.0)
                return await fetch_page_with_mirror_rotation(client, page, end_date_str, attempt + 1)
            else:
                raise Exception(f"HTTP {response.status_code}")
                
        return response.json().get("records", [])
        
    except Exception as e:
        if attempt <= 10:
            logger.warning(f"⚠️ Erro na pág {page}: {e}. Retentando em 3s...")
            await asyncio.sleep(3.0)
            return await fetch_page_with_mirror_rotation(client, page, end_date_str, attempt + 1)
        else:
            raise e

async def fetch_120_days_history():
    logger.info("="*60)
    logger.info("🚀 INICIANDO RESGATE DOS 60 AOS 120 DIAS ATRÁS...")
    logger.info("="*60)
    
    # Define a data 60 dias atrás para usar como 'endDate' (ponto de partida)
    sixty_days_ago = datetime.now(timezone.utc) - timedelta(days=60)
    end_date_str = sixty_days_ago.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
    logger.info(f"📅 Data inicial (endDate da API): {end_date_str}")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    total_saved = 0
    # 60 dias a mais = ~172.800 pedras -> 1.728 páginas
    # Vamos buscar 1.800 páginas apenas para garantir uma margem.
    pages_to_fetch = 1800 
    
    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        for page in range(1, pages_to_fetch + 1):
            records = await fetch_page_with_mirror_rotation(client, page, end_date_str)
            
            if not records:
                logger.warning(f"Página {page} vazia. Chegamos ao limite máximo da Blaze?")
                break
                
            db_records = []
            for item in records:
                try:
                    ts_str = item.get("created_at")
                    utc_dt = datetime.strptime(ts_str.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
                    br_time = utc_dt
                except Exception:
                    br_time = datetime.now(timezone.utc)
                    
                db_records.append({
                    "id": str(item["id"]),
                    "color": format_color(item["color"]),
                    "roll": item["roll"],
                    "timestamp": br_time,
                    "total_bets": 0.0,
                    "total_payout": 0.0,
                    "house_profit": 0.0
                })
                
            await save_results_batch(db_records)
            total_saved += len(db_records)
            
            if page % 25 == 0 or page == pages_to_fetch:
                logger.info(f"📊 Página {page}/{pages_to_fetch} salva. Total extraído hoje: {total_saved} pedras. Último registro lido: {db_records[-1]['timestamp']}")
            
            # Delay anti-block
            await asyncio.sleep(1.0)
            
    logger.info("="*60)
    logger.info(f"🎉 Extração finalizada! Foram salvas {total_saved} pedras mais antigas no banco.")

if __name__ == "__main__":
    asyncio.run(fetch_120_days_history())
