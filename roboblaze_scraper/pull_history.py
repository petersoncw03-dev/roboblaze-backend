import asyncio
import httpx
from datetime import datetime, timezone
import json
import logging
import sys

from database import init_db, AsyncSessionLocal, Result
from sqlalchemy.dialects.postgresql import insert

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("HistoryPuller")

async def save_results_batch(results: list):
    if not results: return
    async with AsyncSessionLocal() as session:
        stmt = insert(Result).values(results)
        stmt = stmt.on_conflict_do_nothing(index_elements=['id'])
        res = await session.execute(stmt)
        await session.commit()
        return res.rowcount

def format_color(color_int: int) -> str:
    if color_int == 0: return "BRANCO"
    elif color_int == 1: return "VERMELHO"
    elif color_int == 2: return "PRETO"
    return "UNKNOWN"

async def pull_history(max_pages=1500):
    await init_db()
    
    total_inserted = 0
    
    async with httpx.AsyncClient() as client:
        # Puxa 20 por página, então 1500 páginas = 30.000 rodadas
        for page in range(1, max_pages + 1):
            url = f"https://blaze.bet.br/api/singleplayer-originals/originals/roulette_games/recent/history/1?page={page}"
            try:
                resp = await client.get(url, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    records = data.get("records", [])
                    if not records:
                        logger.info(f"Fim do histórico na página {page}.")
                        break
                        
                    batch = []
                    for record in records:
                        try:
                            ts_str = record.get("created_at")
                            dt_timestamp = datetime.strptime(ts_str.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
                        except:
                            dt_timestamp = datetime.now(timezone.utc)
                            
                        batch.append({
                            "id": str(record.get("id")),
                            "color": format_color(record.get("color")),
                            "roll": record.get("roll"),
                            "timestamp": dt_timestamp,
                            "total_bets": 0,
                            "total_payout": 0,
                            "house_profit": 0
                        })
                        
                    inserted = await save_results_batch(batch)
                    total_inserted += inserted
                    logger.info(f"Página {page}/{max_pages}: +{inserted} inseridos (Total: {total_inserted})")
                else:
                    logger.warning(f"Erro {resp.status_code} na página {page}. Pausando 5s...")
                    await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Erro de rede na página {page}: {e}")
                await asyncio.sleep(5)
                
            await asyncio.sleep(0.5) # Pausa para não tomar block
            
    logger.info(f"Finalizado! Total de novas rodadas inseridas: {total_inserted}")

if __name__ == "__main__":
    asyncio.run(pull_history())
