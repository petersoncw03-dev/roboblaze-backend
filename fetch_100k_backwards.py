import asyncio
import httpx
from datetime import datetime, timezone, timedelta
import logging
from sqlalchemy.dialects.postgresql import insert
import os
import time
from dotenv import load_dotenv

# Importa as configurações do banco de dados do main/database
from database import AsyncSessionLocal, Result

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Fetch100kBackwards")

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
        # on_conflict_do_nothing GARANTE que não vai dar erro se a pedra já existir, e NÃO vai apagar nada!
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
                logger.warning(f"⚠️ [WARNING] Página {page} retornou HTTP 429. Aguardando {delay}s...")
                await asyncio.sleep(delay)
                return await fetch_page_with_mirror_rotation(client, page, end_date_str, attempt + 1)
            else:
                raise Exception("HTTP Status 429 após 15 tentativas.")
                
        if response.status_code != 200:
            if attempt <= 10:
                logger.warning(f"⚠️ [WARNING] HTTP {response.status_code} na pág {page}. Retentando em 3s...")
                await asyncio.sleep(3.0)
                return await fetch_page_with_mirror_rotation(client, page, end_date_str, attempt + 1)
            else:
                raise Exception(f"HTTP Status {response.status_code} na página {page}.")
                
        data = response.json()
        return data.get("records", [])
    except Exception as e:
        if attempt <= 10:
            logger.warning(f"⚠️ Erro de conexão: {e}. Retentando em 3s...")
            await asyncio.sleep(3.0)
            return await fetch_page_with_mirror_rotation(client, page, end_date_str, attempt + 1)
        else:
            raise e

async def fetch_100k_backwards():
    logger.info("="*60)
    logger.info("🚀 INICIANDO O RESGATE DE 100.000 PEDRAS ANTIGAS (PULANDO AS PRIMEIRAS 160.000)...")
    logger.info("="*60)
    
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    total_saved = 0
    start_page = 1601 # Pula 160.000 (1600 páginas * 100 pedras)
    end_page = 2600   # Mais 1000 páginas = +100.000 pedras (Até 260k total)
    
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        for page in range(start_page, end_page + 1):
            records = await fetch_page_with_mirror_rotation(client, page, now_iso)
            
            if not records:
                logger.warning(f"Página {page} veio vazia. Provavelmente acabou o limite de histórico da Blaze.")
                break
                
            db_records = []
            for item in records:
                try:
                    ts_str = item.get("created_at")
                    utc_dt = datetime.strptime(ts_str.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
                except Exception:
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
            
            if page % 25 == 0 or page == end_page:
                logger.info(f"📊 Página {page}/{end_page} processada. Total de antigas salvas: {total_saved} pedras.")
            
            # Rate limit gentil
            await asyncio.sleep(1.5)
            
    logger.info("="*60)
    logger.info(f"🎉 Resgate concluído! {total_saved} pedras antigas anexadas ao banco sem apagar os dados atuais.")
    logger.info("="*60)

if __name__ == "__main__":
    asyncio.run(fetch_100k_backwards())
