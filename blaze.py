"""
blaze.py — Monitor de Resultados da Blaze via HTTP Polling
Usa o mesmo endpoint REST dos scripts de fetch histórico, que já provaram funcionar.
Salva cada resultado no PostgreSQL e dispara o motor de sinais.
"""

import os
import sys
import time
import json
import requests
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

# Garante que o diretório do projeto está no path
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.append(project_root)

load_dotenv(os.path.join(project_root, ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("BlazeMonitor")

# ── Configurações ──────────────────────────────────────────
MIRRORS = [
    "blaze.bet.br",
    "blaze-6.com",
    "blaze-7.com",
    "blaze-8.com",
    "blaze-9.com",
    "blaze-10.com",
]
POLL_INTERVAL = 8  # segundos entre cada consulta
# ──────────────────────────────────────────────────────────

def format_color(color_int: int) -> str:
    if color_int == 0:   return "BRANCO"
    elif color_int == 1: return "VERMELHO"
    elif color_int == 2: return "PRETO"
    return "UNKNOWN"

def fetch_latest(mirror_idx: int = 0) -> tuple[list, int]:
    """Busca a última página de resultados. Retorna (results, novo_mirror_idx)."""
    domain = MIRRORS[mirror_idx % len(MIRRORS)]
    url = (
        f"https://{domain}/api/singleplayer-originals/originals/"
        f"roulette_games/recent/history/1?page=1"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Referer": f"https://{domain}/pt/games/double",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            records = data.get("records", [])
            return records, mirror_idx
        elif resp.status_code == 429:
            logger.warning(f"⚠️  Rate limit em {domain}. Trocando mirror...")
            return [], (mirror_idx + 1) % len(MIRRORS)
        else:
            logger.warning(f"⚠️  HTTP {resp.status_code} em {domain}")
            return [], mirror_idx
    except Exception as e:
        logger.error(f"❌ Erro ao buscar {domain}: {e}")
        return [], (mirror_idx + 1) % len(MIRRORS)

def save_and_notify(r_id: str, color_str: str, roll: int, created_at: str):
    """Salva no PostgreSQL e dispara motor de sinais."""
    try:
        from roboblaze_api.db import get_conn
        from roboblaze_api.detector import check_user_signals

        conn = get_conn(dict_cursor=False)
        cur = conn.cursor()

        # Parse timestamp
        try:
            utc = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except Exception:
            utc = datetime.now(timezone.utc)

        cur.execute("""
            INSERT INTO results (id, color, roll, timestamp)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (r_id, color_str, roll, utc))

        inserted = cur.rowcount > 0

        if inserted:
            payload_str = json.dumps({
                "id": r_id,
                "color": color_str,
                "roll": roll,
                "timestamp": utc.isoformat()
            })
            cur.execute(f"NOTIFY nova_pedra, '{payload_str}'")

        conn.commit()

        if inserted:
            # Buscar histórico para o detector de sinais
            cur.execute(
                "SELECT roll, color FROM results ORDER BY timestamp DESC LIMIT 50"
            )
            rows = cur.fetchall()
            last_rolls = [{"roll": r[0], "color": r[1]} for r in rows]
            cur.close()
            conn.close()
            check_user_signals(last_rolls)
        else:
            cur.close()
            conn.close()

    except Exception as e:
        logger.error(f"⚠️  Erro DB/Sinais: {e}")

def run():
    logger.info("🚀 BlazeMonitor iniciado (HTTP Polling Mode)")
    logger.info(f"🔄 Intervalo de consulta: {POLL_INTERVAL}s")

    seen_ids: set[str] = set()
    mirror_idx = 0
    errors_consecutive = 0

    while True:
        try:
            records, mirror_idx = fetch_latest(mirror_idx)

            if records:
                errors_consecutive = 0
                for record in records[:5]:  # processa os 5 mais recentes
                    r_id    = str(record.get("id", ""))
                    color   = record.get("color")
                    roll    = record.get("roll")
                    created = record.get("created_at", "")

                    if not r_id or color is None or roll is None:
                        continue

                    if r_id in seen_ids:
                        continue

                    seen_ids.add(r_id)
                    color_str = format_color(color)
                    emoji = {"BRANCO": "⚪", "VERMELHO": "🔴", "PRETO": "⚫"}.get(color_str, "❓")

                    logger.info(f"💎 NOVA PEDRA: {emoji} {color_str} | Roll: {roll} | ID: {r_id}")
                    save_and_notify(r_id, color_str, roll, created)

                # Limitar tamanho do cache de IDs vistos
                if len(seen_ids) > 500:
                    seen_ids = set(list(seen_ids)[-200:])
            else:
                errors_consecutive += 1
                if errors_consecutive >= 5:
                    logger.warning("⚠️  5 falhas consecutivas. Aguardando 30s...")
                    time.sleep(30)
                    errors_consecutive = 0

        except Exception as e:
            logger.error(f"❌ Erro no loop principal: {e}")
            time.sleep(15)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
