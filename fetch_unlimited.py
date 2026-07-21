import asyncio
import httpx
from datetime import datetime, timezone, timedelta
import logging
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import select
import os

from database import AsyncSessionLocal, Result
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("FetchUnlimited")

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
        stmt = stmt.on_conflict_do_nothing(index_elements=['id'])
        await session.execute(stmt)
        await session.commit()

async def get_oldest_timestamp():
    async with AsyncSessionLocal() as session:
        stmt = select(Result.timestamp).order_by(Result.timestamp.asc()).limit(1)
        res = await session.execute(stmt)
        return res.scalar()

async def fetch_page(client, page, start_iso, end_iso, attempt=1):
    domain = MIRRORS[0]
    url = f"https://{domain}/api/singleplayer-originals/originals/roulette_games/recent/history/1?startDate={start_iso}&endDate={end_iso}&page={page}"
    
    try:
        response = await client.get(url)
        if response.status_code == 429:
            if attempt <= 15:
                delay = 3.0 * attempt
                logger.warning(f"⚠️ Rate limit 429. Aguardando {delay}s...")
                await asyncio.sleep(delay)
                return await fetch_page(client, page, start_iso, end_iso, attempt + 1)
            else:
                raise Exception("HTTP 429 após 15 tentativas.")
                
        if response.status_code != 200:
            if attempt <= 10:
                logger.warning(f"⚠️ HTTP {response.status_code}. Retentando em 3s...")
                await asyncio.sleep(3.0)
                return await fetch_page(client, page, start_iso, end_iso, attempt + 1)
            else:
                raise Exception(f"HTTP {response.status_code}")
                
        data = response.json()
        return data.get("records", [])
    except Exception as e:
        if attempt <= 10:
            logger.warning(f"⚠️ Erro: {e}. Retentando em 3s...")
            await asyncio.sleep(3.0)
            return await fetch_page(client, page, start_iso, end_iso, attempt + 1)
        else:
            raise e

async def run_fetch():
    logger.info("="*60)
    logger.info("🚀 INICIANDO RESGATE ILIMITADO (Usando startDate e endDate)")
    logger.info("="*60)
    
    oldest_ts = await get_oldest_timestamp()
    if not oldest_ts:
        oldest_ts = datetime.now(timezone.utc)
    else:
        # Se veio como naive, converte para UTC
        if oldest_ts.tzinfo is None:
            oldest_ts = oldest_ts.replace(tzinfo=timezone.utc)
            
    logger.info(f"📌 Pedra mais antiga no banco: {oldest_ts.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    
    target_stones = 100000
    total_saved = 0
    
    current_end = oldest_ts
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        while total_saved < target_stones:
            # Janela de 5 dias
            current_start = current_end - timedelta(days=5)
            
            end_iso = current_end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            start_iso = current_start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            
            logger.info(f"📅 Buscando Janela: {start_iso} até {end_iso}")
            
            page = 1
            empty_pages = 0
            
            while True:
                records = await fetch_page(client, page, start_iso, end_iso)
                
                if not records:
                    empty_pages += 1
                    if empty_pages >= 2: # Confirma fim da janela
                        break
                    page += 1
                    continue
                
                empty_pages = 0
                db_records = []
                for item in records:
                    try:
                        ts_str = item.get("created_at")
                        utc_dt = datetime.strptime(ts_str.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
                    except:
                        utc_dt = datetime.now(timezone.utc)
                        
                    db_records.append({
                        "id": str(item["id"]),
                        "color": format_color(item["color"]),
                        "roll": item["roll"],
                        "timestamp": utc_dt,
                        "total_bets": 0.0,
                        "total_payout": 0.0,
                        "house_profit": 0.0
                    })
                    
                await save_results_batch(db_records)
                total_saved += len(db_records)
                
                if page % 10 == 0:
                    logger.info(f"📊 Janela atual - Pág {page}. Total global resgatado: {total_saved}/{target_stones}")
                    
                if total_saved >= target_stones:
                    break
                    
                page += 1
                await asyncio.sleep(1.0) # Rate limit
                
            # Move a janela
            current_end = current_start
            
    logger.info("="*60)
    logger.info(f"🎉 Resgate ilimitado concluído! {total_saved} pedras salvas.")
    logger.info("="*60)

if __name__ == "__main__":
    asyncio.run(run_fetch())
