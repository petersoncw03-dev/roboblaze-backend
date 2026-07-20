import asyncio
import json
import logging
from datetime import datetime, timezone
import websockets
import httpx
from fastapi import FastAPI
from contextlib import asynccontextmanager
from sqlalchemy.future import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import delete
import os
from dotenv import load_dotenv
import ssl

def get_proxy_url():
    host = os.getenv("PROXY_HOST")
    port = os.getenv("PROXY_PORT")
    user = os.getenv("PROXY_USER")
    password = os.getenv("PROXY_PASS")
    if host and port:
        if user and password:
            return f"http://{user}:{password}@{host}:{port}"
        return f"http://{host}:{port}"
    return None

def get_ssl_context():
    ssl_context = ssl.create_default_context()
    # TLS Spoofing Básico para bypass de Datacenter/Cloudflare
    ssl_context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
    ssl_context.set_ciphers("DEFAULT@SECLEVEL=1")
    return ssl_context

from database import init_db, AsyncSessionLocal, Result

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ScraperWorker")

# WebSocket usa blaze.com (funciona, só o REST que retorna 2024)
# REST do auditor usa blaze.bet.br (dados 2026, mas history vazio — só WS resolve)
BLAZE_WS_URL = "wss://api-gaming.blaze.bet.br/replication/?EIO=3&transport=websocket"
BLAZE_HISTORY_URL = "https://blaze.bet.br/api/singleplayer-originals/originals/roulette_games/recent/history/1?page=1"

ROOMS = ["double_room_1"]

# =============================================================================
# FUNÇÕES DE BANCO DE DADOS
# =============================================================================
async def purge_old_data():
    """Remove dados de 2024 ou anteriores que podem estar poluindo o banco"""
    async with AsyncSessionLocal() as session:
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        stmt = delete(Result).where(Result.timestamp < cutoff)
        result = await session.execute(stmt)
        await session.commit()
        if result.rowcount > 0:
            logger.warning(f"🧹 [CLEANUP] Removidos {result.rowcount} registros antigos (pré-2026) do banco.")

async def save_results_batch(results: list):
    if not results: return
    async with AsyncSessionLocal() as session:
        stmt = insert(Result).values(results)
        stmt = stmt.on_conflict_do_nothing(index_elements=['id'])
        res = await session.execute(stmt)
        await session.commit()
        
        # Otimização HFT: Dispara evento realtime para o SSE do Next.js
        if res.rowcount > 0:
            from sqlalchemy import text
            for r in results:
                payload_str = json.dumps({
                    "id": r["id"],
                    "color": r["color"],
                    "roll": r["roll"],
                    "timestamp": r["timestamp"].isoformat(),
                    "total_bets": float(r.get("total_bets", 0) or 0),
                    "total_payout": float(r.get("total_payout", 0) or 0),
                    "house_profit": float(r.get("house_profit", 0) or 0)
                })
                # Evitando SQL Injection garantindo que a string gerada por json.dumps é segura para literal
                await session.execute(text(f"NOTIFY nova_pedra, '{payload_str}'"))
            await session.commit()

async def get_latest_db_record():
    async with AsyncSessionLocal() as session:
        stmt = select(Result).order_by(Result.timestamp.desc()).limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

async def count_records():
    async with AsyncSessionLocal() as session:
        from sqlalchemy import func
        result = await session.execute(select(func.count(Result.id)))
        return result.scalar()

def format_color(color_int: int) -> str:
    if color_int == 0: return "BRANCO"
    elif color_int == 1: return "VERMELHO"
    elif color_int == 2: return "PRETO"
    return "UNKNOWN"

# =============================================================================
# WORKER A: WEBSOCKET (COLETOR EM TEMPO REAL)
# WebSocket usa a porta VIP
BLAZE_WS_URL = "wss://api-gaming.blaze.bet.br/replication/?EIO=3&transport=websocket"

async def auto_refresh_token():
    """Renova o BLAZE_ACCESS_TOKEN automaticamente usando o BLAZE_REFRESH_TOKEN a cada 11 horas."""
    while True:
        await asyncio.sleep(11 * 3600)  # 11 horas
        refresh_token = os.getenv("BLAZE_REFRESH_TOKEN")
        if not refresh_token:
            logger.warning("[Worker A] Nenhum BLAZE_REFRESH_TOKEN configurado. Ignorando renovação automática.")
            continue
            
        try:
            logger.info("[Worker A] 🔄 Solicitando novo Access Token para a Blaze...")
            proxy_url = get_proxy_url()
            async with httpx.AsyncClient(proxy=proxy_url) as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Content-Type": "application/json"
                }
                payload = {"refresh_token": refresh_token}
                resp = await client.put("https://blaze.bet.br/api/auth/token", json=payload, headers=headers, timeout=10)
                
                if resp.status_code in [200, 201]:
                    data = resp.json()
                    if "access_token" in data:
                        # Atualiza a variavel de ambiente em memoria para o proximo ciclo
                        os.environ["BLAZE_ACCESS_TOKEN"] = data["access_token"]
                        logger.info("[Worker A] ✅ Token renovado com sucesso!")
                else:
                    logger.error(f"[Worker A] ❌ Falha ao renovar token. Status: {resp.status_code}")
        except Exception as e:
            logger.error(f"[Worker A] ❌ Erro de rede ao renovar token: {e}")

async def send_heartbeat(ws):
    try:
        while True:
            await asyncio.sleep(20)
            await ws.send("2")
            logger.info("[Worker A] Heartbeat ♥")
    except Exception:
        pass

async def worker_a_websocket():
    logger.info(f"[Worker A] Iniciando WebSocket em: {BLAZE_WS_URL}")
    last_saved_id = None
    asyncio.create_task(auto_refresh_token())
    
    while True:
        heartbeat_task = None
        try:
            extra_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Origin": "https://blaze.bet.br"
            }
            
            ssl_context = get_ssl_context()
            
            # Conexão WS com TLS Spoofing
            async with websockets.connect(BLAZE_WS_URL, extra_headers=extra_headers, ssl=ssl_context) as ws:
                logger.info("[Worker A] ✅ Conectado ao WebSocket com TLS Spoofing ativo.")
                heartbeat_task = asyncio.create_task(send_heartbeat(ws))
                subscribed = False
                
                while True:
                    msg = await ws.recv()
                    
                    if msg.startswith("0") and not subscribed:
                        await ws.send("40")
                        logger.info("[Worker A] -> Enviado '40' (upgrade para Socket.IO)")
                    
                    elif msg.startswith("40"):
                        token = os.getenv("BLAZE_ACCESS_TOKEN")
                        if token:
                            logger.info("[Worker A] 🔑 Enviando Payload de Autenticação Vip (Token)...")
                            await ws.send(f'420["cmd",{{"id":"authenticate","payload":{{"token":"{token}"}}}}]')
                            await ws.send(f'421["cmd",{{"id":"trackTime","payload":{{"token":"{token}"}}}}]')

                        for i, room in enumerate(ROOMS):
                            sub = f'42{i+2}["cmd",{{"id":"subscribe","payload":{{"room":"{room}"}}}}]'
                            await ws.send(sub)
                            logger.info(f"[Worker A] -> Inscrito na sala: {room}")
                        subscribed = True
                        logger.info("[Worker A] 🎯 Todas as inscrições feitas! Aguardando pedras...")
                    
                    elif msg == "2":
                        await ws.send("3")
                    
                    elif msg.startswith("42"):
                        try:
                            data = json.loads(msg[2:])
                            event_name = data[0]
                            payload = data[1] if len(data) > 1 else {}
                            
                            if event_name == "data":
                                inner_event = payload.get("id")
                                inner_payload = payload.get("payload", {})
                                
                                if inner_event in ["double.tick", "double.update"]:
                                    status = inner_payload.get("status")
                                    color_int = inner_payload.get("color")
                                    roll = inner_payload.get("roll")
                                    r_id = inner_payload.get("id")
                                    
                                    if status in ["rolling", "complete"] and r_id and color_int is not None and roll is not None:
                                        if str(r_id) != last_saved_id:
                                            last_saved_id = str(r_id)
                                            
                                            # Blaze envia os totais agrupados por cor
                                            t_red = float(inner_payload.get("total_red_eur_bet", 0) or 0)
                                            t_white = float(inner_payload.get("total_white_eur_bet", 0) or 0)
                                            t_black = float(inner_payload.get("total_black_eur_bet", 0) or 0)
                                            
                                            calc_total_bets = t_red + t_white + t_black
                                            
                                            if color_int == 0:
                                                calc_total_payout = t_white * 14
                                            elif color_int == 1:
                                                calc_total_payout = t_red * 2
                                            elif color_int == 2:
                                                calc_total_payout = t_black * 2
                                            else:
                                                calc_total_payout = 0
                                                
                                            try:
                                                ts_str = inner_payload.get("created_at")
                                                dt_timestamp = datetime.strptime(ts_str.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
                                            except Exception:
                                                dt_timestamp = datetime.now(timezone.utc)
                                                
                                            record = {
                                                "id": str(r_id),
                                                "color": format_color(color_int),
                                                "roll": roll,
                                                "timestamp": dt_timestamp,
                                                "total_bets": calc_total_bets,
                                                "total_payout": calc_total_payout,
                                                "house_profit": calc_total_bets - calc_total_payout
                                            }
                                            await save_results_batch([record])
                                            total = await count_records()
                                            logger.info(f"[Worker A] 🎲 PEDRA SALVA: {record['color']} (Nº {roll}) | Lucro da Casa: {record['house_profit']:.2f} | Total no banco: {total}")
                                    
                        except Exception as e:
                            logger.warning(f"[Worker A] Erro ao processar msg: {e}")
                    
        except Exception as e:
            logger.warning(f"[Worker A] Conexão perdida: {e}. Reconectando em 5s...")
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
        await asyncio.sleep(5)

# =============================================================================
# WORKER B: AUDITOR DE SINCRONIZAÇÃO
# =============================================================================
async def worker_b_auditor():
    logger.info("[Worker B] Auditor iniciado. Primeira auditoria em 30s...")
    await asyncio.sleep(30)
    
    while True:
        try:
            logger.info("[Worker B] Executando Auditoria...")
            total = await count_records()
            logger.info(f"[Worker B] Total de pedras no banco: {total}")
            
        except Exception as e:
            logger.error(f"[Worker B] Erro na auditoria: {e}")
            
        await asyncio.sleep(600)

# =============================================================================
# FASTAPI APP COM LIFESPAN
# =============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await purge_old_data()
    
    task_a = asyncio.create_task(worker_a_websocket())
    task_b = asyncio.create_task(worker_b_auditor())
    yield
    task_a.cancel()
    task_b.cancel()

app = FastAPI(lifespan=lifespan, title="RoboBlaze Scraper 2026")

@app.get("/")
def healthcheck():
    return {"status": "online", "message": "RoboBlaze Scraper 2026 rodando!"}
