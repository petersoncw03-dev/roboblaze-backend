import asyncio
import httpx
from datetime import datetime, timezone, timedelta
import logging
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import text
import os
import time
from dotenv import load_dotenv

# Importa as configurações do banco de dados do main/database
from database import AsyncSessionLocal, Result

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Fetch90k")

# Lista de espelhos da Blaze para contornar qualquer limite da Cloudflare (HTTP 429)
MIRRORS = [
    "blaze-6.com",
    "blaze-7.com",
    "blaze-8.com",
    "blaze-9.com",
    "blaze-10.com",
    "blaze.com",
    "blaze.bet.br"
]
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

async def fetch_page_with_mirror_rotation(client, page, end_date_str, attempt=1):
    global current_mirror_idx
    
    domain = MIRRORS[current_mirror_idx]
    url = f"https://{domain}/api/singleplayer-originals/originals/roulette_games/recent/history/1?endDate={end_date_str}&page={page}"
    
    try:
        response = await client.get(url)
        
        # Se for rate limited (429), rotacionamos para o próximo mirror
        if response.status_code == 429:
            if attempt <= 10:
                old_domain = domain
                current_mirror_idx = (current_mirror_idx + 1) % len(MIRRORS)
                new_domain = MIRRORS[current_mirror_idx]
                logger.warning(f"⚠️ [WARNING] Página {page} retornou HTTP 429 em {old_domain}. Rotacionando para mirror {new_domain} (Tentativa {attempt}/10 em 1.5s)...")
                await asyncio.sleep(1.5)
                return await fetch_page_with_mirror_rotation(client, page, end_date_str, attempt + 1)
            else:
                raise Exception(f"HTTP Status 429 (Rate Limit) após 10 rotações de espelho.")
                
        if response.status_code != 200:
            if attempt <= 5:
                current_mirror_idx = (current_mirror_idx + 1) % len(MIRRORS)
                next_domain = MIRRORS[current_mirror_idx]
                logger.warning(f"⚠️ [WARNING] Página {page} retornou HTTP {response.status_code} em {domain}. Rotacionando para mirror {next_domain} em 1.5s...")
                await asyncio.sleep(1.5)
                return await fetch_page_with_mirror_rotation(client, page, end_date_str, attempt + 1)
            else:
                raise Exception(f"HTTP Status {response.status_code} na página {page}.")
                
        data = response.json()
        return data.get("records", [])
        
    except Exception as e:
        if attempt <= 5:
            current_mirror_idx = (current_mirror_idx + 1) % len(MIRRORS)
            next_domain = MIRRORS[current_mirror_idx]
            logger.warning(f"⚠️ [WARNING] Erro de conexão em {domain}: {e}. Rotacionando para mirror {next_domain} em 1.5s...")
            await asyncio.sleep(1.5)
            return await fetch_page_with_mirror_rotation(client, page, end_date_str, attempt + 1)
        else:
            raise e

async def fetch_90k_history():
    logger.info("="*60)
    logger.info("🚀 [MIGRAÇÃO SCRAPER] INICIANDO O RESGATE DE 90.000 PEDRAS ANTIGAS...")
    logger.info("="*60)
    
    # 1. Limpar tabela results por completo antes de começar
    logger.info("🧹 Limpando tabela de resultados no PostgreSQL para alinhamento 100% limpo...")
    async with AsyncSessionLocal() as session:
        await session.execute(text("TRUNCATE TABLE results;"))
        await session.commit()
    logger.info("✅ Tabela results limpa com sucesso.")
    
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    total_saved = 0
    pages_to_fetch = 900 # 900 páginas * 100 registros = 90.000 pedras
    
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        for page in range(1, pages_to_fetch + 1):
            records = await fetch_page_with_mirror_rotation(client, page, now_iso)
            
            if not records:
                logger.warning(f"Página {page} veio vazia. Parando busca.")
                break
                
            db_records = []
            for item in records:
                try:
                    ts_str = item.get("created_at")
                    # Preservar o timestamp oficial UTC para inserção perfeita no banco timezone-aware
                    utc_dt = datetime.strptime(ts_str.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
                    br_time = utc_dt
                except Exception:
                    br_time = datetime.now(timezone.utc) if 'timezone' in globals() else datetime.now()
                    
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
                logger.info(f"📊 Página {page}/{pages_to_fetch} salva. Total até agora: {total_saved} pedras.")
            
            # Pequeno delay para não sobrecarregar a Blaze
            await asyncio.sleep(0.15)
            
    logger.info("="*60)
    logger.info(f"🎉 Resgate concluído! {total_saved} pedras antigas salvas em BRT com sucesso no banco.")
    logger.info("="*60)

if __name__ == "__main__":
    asyncio.run(fetch_90k_history())

