"""
blaze.py — Monitor em tempo real via WebSocket
O token é enviado DENTRO do payload da subscription (formato atual da Blaze).
Fallback automático para HTTP se o WebSocket falhar por 2 minutos.
"""
import os, sys, time, json, ssl, threading, logging, requests
import websocket
from datetime import datetime, timezone
from dotenv import load_dotenv

project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.append(project_root)

load_dotenv(os.path.join(project_root, ".env"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger("BlazeMonitor")

MIRRORS = ["blaze.bet.br", "blaze-6.com", "blaze-7.com", "blaze-8.com"]
WS_URL  = "wss://api-v2.blaze.bet.br/replication/?EIO=3&transport=websocket"

def format_color(c):
    return {0: "BRANCO", 1: "VERMELHO", 2: "PRETO"}.get(c, "UNKNOWN")

def save_and_notify(r_id, color_str, roll, created_at, wagered=None, winnings=None, profit=None):
    try:
        from roboblaze_api.db import get_conn
        from roboblaze_api.detector import check_user_signals

        conn = get_conn(dict_cursor=False)
        cur  = conn.cursor()
        try:
            utc = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except Exception:
            utc = datetime.now(timezone.utc)

        cur.execute("""
            INSERT INTO results (id, color, roll, timestamp, wagered, winnings, profit)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (r_id, color_str, roll, utc, wagered, winnings, profit))

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


class BlazeMonitor:
    def __init__(self):
        self.seen_ids    = set()
        self.token       = os.getenv("BLAZE_ACCESS_TOKEN", "")
        self.ws          = None
        self.last_stone  = time.time()  # rastreia última pedra recebida
        self.running     = True

    # ── WebSocket handlers ──────────────────────────────────────────────────

    def on_open(self, ws):
        logger.info("🌐 WebSocket conectado.")
        # Keepalive ping a cada 25s
        def ping():
            while ws.keep_running:
                time.sleep(25)
                try: ws.send("2")
                except: break
        threading.Thread(target=ping, daemon=True).start()

    def on_message(self, ws, message):
        # Ping/Pong do socket.io
        if message == "2": ws.send("3"); return
        if message == "3": return

        # Handshake inicial — conectar ao namespace
        if message.startswith("0"):
            ws.send("40")
            return

        # Namespace conectado — assinar as salas
        if message.startswith("40"):
            logger.info("🔌 Namespace conectado. Assinando salas...")

            # Tenta 3 formatos diferentes até algum funcionar
            if self.token:
                # Formatos com autenticação (necessário para salas atuais da Blaze)
                ws.send(f'42["cmd",{{"id":"subscribe","payload":{{"room":"roulette","token":"{self.token}"}}}}]')
                ws.send(f'42["cmd",{{"id":"subscribe","payload":{{"room":"double_v2","token":"{self.token}"}}}}]')
                ws.send(f'42["cmd",{{"id":"subscribe","payload":{{"room":"double_room_1","token":"{self.token}"}}}}]')
                ws.send(f'42["cmd",{{"id":"authenticate","payload":{{"token":"{self.token}"}}}}]')
            
            # Formatos públicos (fallback)
            ws.send('42["cmd",{"id":"subscribe","payload":{"room":"roulette"}}]')
            ws.send('42["cmd",{"id":"subscribe","payload":{"room":"double_v2"}}]')
            logger.info("✅ Assinaturas enviadas (Socket). Aguardando pedras...")
            return

        # Mensagens de evento
        if not message.startswith("42"):
            return

        try:
            raw = message[2:]  # remove prefix "42"
            data = json.loads(raw)
            if not isinstance(data, list) or len(data) < 2:
                return

            event_wrapper = data[1]
            if not isinstance(event_wrapper, dict):
                return

            event_id = event_wrapper.get("id", "")
            payload  = event_wrapper.get("payload", {})

            # Eventos de resultado do jogo Double
            if event_id in ("double.update", "double.tick", "roulette.update"):
                status = payload.get("status")
                if status not in ("rolling", "complete"):
                    return

                r_id   = str(payload.get("id", ""))
                color  = payload.get("color")
                roll   = payload.get("roll")
                created = payload.get("created_at", "")

                if r_id and r_id not in self.seen_ids and color is not None and roll is not None:
                    self.seen_ids.add(r_id)
                    self.last_stone = time.time()
                    color_str = format_color(color)
                    emoji = {"BRANCO": "⚪", "VERMELHO": "🔴", "PRETO": "⚫"}.get(color_str, "❓")
                    logger.info(f"💎 NOVA PEDRA (WebSocket): {emoji} {color_str} | Roll: {roll} | ID: {r_id}")
                    threading.Thread(target=save_and_notify, args=(r_id, color_str, roll, created), daemon=True).start()

                    if len(self.seen_ids) > 500:
                        self.seen_ids = set(list(self.seen_ids)[-200:])

            # Usa wallet.bet-resulted como sinal: rodada acabou → busca via HTTP
            elif event_id == "wallet.bet-resulted":
                game = payload.get("game_slug", "")
                if "double" in game.lower():
                    logger.info("💡 Aposta liquidada detectada. Buscando resultado via HTTP...")
                    # Pass the profit data to the fetch function so it can save it with the latest stone
                    threading.Thread(
                        target=self._fetch_and_save_latest, 
                        kwargs={
                            "wagered": payload.get("wagered"),
                            "winnings": payload.get("winnings"),
                            "profit": payload.get("profit")
                        },
                        daemon=True
                    ).start()

        except Exception:
            pass

    def on_error(self, ws, error):
        logger.error(f"❌ Erro WebSocket: {error}")

    def on_close(self, ws, *args):
        logger.warning("⚠️  WebSocket fechado. Reconectando em 10s...")
        time.sleep(10)
        if self.running:
            self.start_ws()

    # ── HTTP fallback (acionado por wallet.bet-resulted) ───────────────────

    def _fetch_and_save_latest(self, wagered=None, winnings=None, profit=None):
        for domain in MIRRORS:
            try:
                url = f"https://{domain}/api/singleplayer-originals/originals/roulette_games/recent/history/1?page=1"
                r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
                if r.status_code != 200:
                    continue
                records = r.json().get("records", [])
                for record in records[:3]:
                    r_id   = str(record.get("id", ""))
                    color  = record.get("color")
                    roll   = record.get("roll")
                    created = record.get("created_at", "")
                    if r_id and r_id not in self.seen_ids and color is not None and roll is not None:
                        self.seen_ids.add(r_id)
                        self.last_stone = time.time()
                        color_str = format_color(color)
                        emoji = {"BRANCO": "⚪", "VERMELHO": "🔴", "PRETO": "⚫"}.get(color_str, "❓")
                        logger.info(f"💎 NOVA PEDRA (HTTP): {emoji} {color_str} | Roll: {roll} | ID: {r_id}")
                        save_and_notify(r_id, color_str, roll, created, wagered, winnings, profit)
                return
            except Exception:
                continue

    # ── Watchdog & Polling Silencioso ──────────────────────────────────────

    def _watchdog(self):
        while self.running:
            time.sleep(5) # A cada 5 segundos verifica se saiu pedra nova por HTTP
            
            # Se já passaram 5 segundos desde a última pedra, tentamos buscar via HTTP
            # Isso garante que se o WebSocket não estiver enviando os eventos, ainda pegamos a pedra
            if time.time() - self.last_stone >= 5:
                # Não bloqueia o watchdog, faz numa thread rápida
                threading.Thread(target=self._fetch_and_save_latest, daemon=True).start()

            # Reconexão forçada se passar de 3 min
            if time.time() - self.last_stone > 180:
                logger.warning("🔁 Sem pedras há 3 min. Forçando reconexão do Socket...")
                self.last_stone = time.time()
                try:
                    if self.ws:
                        self.ws.close()
                except Exception:
                    pass

    # ── Start ──────────────────────────────────────────────────────────────

    def start_ws(self):
        # Garante que as colunas existam no banco
        try:
            from roboblaze_api.db import get_conn
            conn = get_conn(dict_cursor=False)
            cur = conn.cursor()
            cur.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS wagered NUMERIC(15,2);")
            cur.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS winnings NUMERIC(15,2);")
            cur.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS profit NUMERIC(15,2);")
            
            # Criar VIEW de lucro diário para facilitar o gráfico do frontend
            cur.execute("""
                CREATE OR REPLACE VIEW daily_pnl AS
                SELECT 
                    DATE(timestamp) AS day,
                    SUM(wagered) AS total_wagered,
                    SUM(winnings) AS total_winnings,
                    SUM(profit) AS net_profit,
                    COUNT(*) AS rounds
                FROM results 
                WHERE profit IS NOT NULL
                GROUP BY DATE(timestamp)
                ORDER BY day DESC;
            """)
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.error(f"⚠️ Erro ao atualizar schema DB: {e}")

        headers = {
            "Origin": "https://blaze.bet.br",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.set_ciphers("DEFAULT@SECLEVEL=1")

        self.ws = websocket.WebSocketApp(
            WS_URL,
            header=headers,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
        )
        self.ws.run_forever(sslopt={"context": ssl_ctx})

    def start(self):
        logger.info("🚀 BlazeMonitor iniciado (WebSocket Real-Time + HTTP fallback)")
        self.last_stone = time.time()
        threading.Thread(target=self._watchdog, daemon=True).start()
        self.start_ws()


if __name__ == "__main__":
    monitor = BlazeMonitor()
    monitor.start()
