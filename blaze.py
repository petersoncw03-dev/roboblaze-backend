"""
blaze.py — Monitor em tempo real via Smart Polling no endpoint /current
Detecta a pedra INSTANTE que o status vira 'complete'.
"""
import os, sys, time, json, logging, requests
from datetime import datetime, timezone
from dotenv import load_dotenv

project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.append(project_root)

load_dotenv(os.path.join(project_root, ".env"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger("BlazeMonitor")

MIRRORS = ["blaze.bet.br", "blaze-6.com", "blaze-7.com", "blaze-8.com", "blaze-9.com"]
CURRENT_ENDPOINT = "/api/singleplayer-originals/originals/roulette_games/current"

def format_color(c):
    return {0: "BRANCO", 1: "VERMELHO", 2: "PRETO"}.get(c, "UNKNOWN")

def fetch_current(mirror_idx=0):
    domain = MIRRORS[mirror_idx % len(MIRRORS)]
    url = f"https://{domain}{CURRENT_ENDPOINT}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": f"https://{domain}/pt/games/double",
    }
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            return r.json(), mirror_idx
        elif r.status_code == 429:
            return None, (mirror_idx + 1) % len(MIRRORS)
        return None, mirror_idx
    except Exception as e:
        logger.error(f"❌ Erro fetch: {e}")
        return None, (mirror_idx + 1) % len(MIRRORS)

def save_and_notify(r_id, color_str, roll, created_at):
    try:
        from roboblaze_api.db import get_conn
        from roboblaze_api.detector import check_user_signals

        conn = get_conn(dict_cursor=False)
        cur = conn.cursor()
        try:
            utc = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except Exception:
            utc = datetime.now(timezone.utc)

        cur.execute("""
            INSERT INTO results (id, color, roll, timestamp)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (r_id, color_str, roll, utc))

        if cur.rowcount > 0:
            payload_str = json.dumps({"id": r_id, "color": color_str, "roll": roll, "timestamp": utc.isoformat()})
            cur.execute(f"NOTIFY nova_pedra, '{payload_str}'")
            conn.commit()
            cur.execute("SELECT roll, color FROM results ORDER BY timestamp DESC LIMIT 50")
            rows = cur.fetchall()
            last_rolls = [{"roll": r[0], "color": r[1]} for r in rows]
            cur.close(); conn.close()
            check_user_signals(last_rolls)
        else:
            conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"⚠️  Erro DB/Sinais: {e}")

def run():
    logger.info("🚀 BlazeMonitor iniciado (Smart Polling — detecção instantânea)")
    seen_ids = set()
    mirror_idx = 0
    last_status = None
    consecutive_errors = 0

    while True:
        data, mirror_idx = fetch_current(mirror_idx)

        if data is None:
            consecutive_errors += 1
            wait = 30 if consecutive_errors >= 5 else 5
            time.sleep(wait)
            continue

        consecutive_errors = 0
        status   = data.get("status", "")
        r_id     = str(data.get("id", ""))
        color    = data.get("color")
        roll     = data.get("roll")
        created  = data.get("created_at", "")

        # Detecta INSTANTE que o resultado fica disponível
        if status == "complete" and r_id and r_id not in seen_ids:
            if color is not None and roll is not None:
                seen_ids.add(r_id)
                color_str = format_color(color)
                emoji = {"BRANCO": "⚪", "VERMELHO": "🔴", "PRETO": "⚫"}.get(color_str, "❓")
                logger.info(f"💎 NOVA PEDRA: {emoji} {color_str} | Roll: {roll} | ID: {r_id}")
                save_and_notify(r_id, color_str, roll, created)
                if len(seen_ids) > 500:
                    seen_ids = set(list(seen_ids)[-200:])

        last_status = status

        # Timing inteligente:
        # - 'rolling' = animação em curso, resultado vem logo → poll rápido (1s)
        # - 'complete' = acabou, próxima rodada demorará → espera 3s
        # - 'waiting'  = aguardando apostas → espera 2s
        if status == "rolling":
            time.sleep(1)
        elif status == "complete":
            time.sleep(3)
        else:
            time.sleep(2)

if __name__ == "__main__":
    run()
