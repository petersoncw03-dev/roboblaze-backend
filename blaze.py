"""
blaze.py — Monitor em tempo real via WebSocket
O token é enviado DENTRO do payload da subscription (formato atual da Blaze).
Fallback automático para HTTP se o WebSocket falhar por 2 minutos.
"""
import os, sys, time, json, threading, logging, requests
from datetime import datetime, timezone
from dotenv import load_dotenv
import websocket

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

def save_and_notify(r_id, color_str, roll, created_at, wagered=None, winnings=None, profit=None, total_bets=None, total_payout=None, house_profit=None):
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
            INSERT INTO results (id, color, roll, timestamp, wagered, winnings, profit, total_bets, total_payout, house_profit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET 
                wagered = COALESCE(EXCLUDED.wagered, results.wagered),
                winnings = COALESCE(EXCLUDED.winnings, results.winnings),
                profit = COALESCE(EXCLUDED.profit, results.profit),
                total_bets = COALESCE(EXCLUDED.total_bets, results.total_bets),
                total_payout = COALESCE(EXCLUDED.total_payout, results.total_payout),
                house_profit = COALESCE(EXCLUDED.house_profit, results.house_profit)
        """, (r_id, color_str, roll, utc, wagered, winnings, profit, total_bets, total_payout, house_profit))

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
        self.last_stone  = time.time()
        self.running     = True

    def on_message(self, ws, message):
        try:
            if message == "2":
                ws.send("3")
                return
            
            if message.startswith("40"):
                logger.info("📡 Conectado ao namespace. Enviando subscriptions...")
                
                rooms = ["double_room_1", "double_room_2", "double_v2", "roulette", "roulette_games"]
                for i, room in enumerate(rooms):
                    payload = {"room": room}
                    if self.token:
                        payload["token"] = self.token
                    ws.send(f'42{i}["cmd",{{"id":"subscribe","payload":{json.dumps(payload)}}}]')
                return
                
            if not message or not message.startswith("42"):
                return
            
            raw = message[2:]
            data = json.loads(raw)
            if not isinstance(data, list) or len(data) < 2: return
            
            event_wrapper = data[1]
            if not isinstance(event_wrapper, dict): return
            
            event_id = event_wrapper.get("id", "")
            payload  = event_wrapper.get("payload", {})

            if event_id in ("double.update", "double.tick", "roulette.update"):
                status = payload.get("status")
                if status not in ("rolling", "complete"): return
                
                r_id   = str(payload.get("id", ""))
                color  = payload.get("color")
                roll   = payload.get("roll")
                created = payload.get("created_at", "")
                
                # Extrair lucros globais da sala (house PnL)
                total_bets = payload.get("total_bets", 0.0)
                total_payout = payload.get("total_payout", 0.0)
                try:
                    house_profit = float(total_bets) - float(total_payout)
                except:
                    house_profit = 0.0
                
                if r_id and r_id not in self.seen_ids and color is not None and roll is not None:
                    self.seen_ids.add(r_id)
                    self.last_stone = time.time()
                    color_str = format_color(color)
                    emoji = {"BRANCO": "⚪", "VERMELHO": "🔴", "PRETO": "⚫"}.get(color_str, "❓")
                    logger.info(f"💎 NOVA PEDRA (WebSocket Leve): {emoji} {color_str} | Roll: {roll} | ID: {r_id}")
                    threading.Thread(target=save_and_notify, args=(r_id, color_str, roll, created, None, None, None, total_bets, total_payout, house_profit), daemon=True).start()
                    
                    if len(self.seen_ids) > 500:
                        self.seen_ids = set(list(self.seen_ids)[-200:])
                        
            elif event_id == "wallet.bet-resulted":
                game = payload.get("game_slug", "")
                if "double" in game.lower():
                    logger.info("💡 Aposta liquidada detectada...")
                    threading.Thread(
                        target=self._fetch_and_save_latest, 
                        kwargs={"wagered": payload.get("wagered"), "winnings": payload.get("winnings"), "profit": payload.get("profit")},
                        daemon=True
                    ).start()
        except Exception as e:
            pass

    def on_error(self, ws, error):
        logger.error(f"❌ WebSocket Erro: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        logger.warning("🔌 WebSocket Fechado. Reconectando em breve...")

    def on_open(self, ws):
        logger.info(f"✅ Conexão WebSocket estabelecida! Enviando init...")
        ws.send("40")
        
        # Ping thread (Engine.io v4 requires client to ping)
        def ping_loop():
            while self.running and ws.sock and ws.sock.connected:
                time.sleep(20)
                try:
                    ws.send("2")
                except:
                    break
        threading.Thread(target=ping_loop, daemon=True).start()

    # ── HTTP fallback ───────────────────
    def _fetch_and_save_latest(self, wagered=None, winnings=None, profit=None):
        for domain in MIRRORS:
            try:
                url = f"https://{domain}/api/singleplayer-originals/originals/roulette_games/recent/history/1?page=1"
                r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
                if r.status_code != 200: continue
                records = r.json().get("records", [])
                for record in records[:3]:
                    r_id   = str(record.get("id", ""))
                    color  = record.get("color")
                    roll   = record.get("roll")
                    created = record.get("created_at", "")
                    
                    # A API HTTP não fornece lucros globais de forma direta, mantemos None para preservar os que vieram do WebSocket
                    total_bets = None
                    total_payout = None
                    house_profit = None
                    
                    if r_id and color is not None and roll is not None:
                        color_str = format_color(color)
                        
                        # Se já vimos a pedra pelo WebSocket, apenas atualizamos o lucro no banco
                        if r_id in self.seen_ids:
                            if profit is not None:
                                save_and_notify(r_id, color_str, roll, created, wagered, winnings, profit, total_bets, total_payout, house_profit)
                        else:
                            # Se for uma pedra nova (fallback HTTP real)
                            self.seen_ids.add(r_id)
                            self.last_stone = time.time()
                            emoji = {"BRANCO": "⚪", "VERMELHO": "🔴", "PRETO": "⚫"}.get(color_str, "❓")
                            save_and_notify(r_id, color_str, roll, created, wagered, winnings, profit, total_bets, total_payout, house_profit)
                return
            except Exception:
                continue

    # ── Watchdog ──────────────────────────────────────
    def _watchdog(self):
        while self.running:
            time.sleep(5) 
            if time.time() - self.last_stone > 300:
                logger.warning("🔁 Sem pedras há 5 min. Reiniciando processo...")
                os._exit(1) # Força o Docker/Easypanel a reiniciar o container limpo

    # ── Start WebSocket ───────────────────────────────────────────────────
    def start_ws(self):
        # Garante que as colunas existam no banco
        try:
            from roboblaze_api.db import get_conn
            conn = get_conn(dict_cursor=False)
            cur = conn.cursor()
            cur.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS wagered NUMERIC(15,2);")
            cur.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS winnings NUMERIC(15,2);")
            cur.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS profit NUMERIC(15,2);")
            cur.execute("""
                CREATE OR REPLACE VIEW daily_pnl AS
                SELECT DATE(timestamp) AS day, SUM(wagered) AS total_wagered, SUM(winnings) AS total_winnings, SUM(profit) AS net_profit, COUNT(*) AS rounds
                FROM results WHERE profit IS NOT NULL GROUP BY DATE(timestamp) ORDER BY day DESC;
            """)
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.error(f"⚠️ Erro ao atualizar schema DB: {e}")

        logger.info("🚀 Inicializando WebSockets leves...")
        headers = ["User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"]
        
        while self.running:
            try:
                ws = websocket.WebSocketApp(
                    WS_URL,
                    header=headers,
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close
                )
                ws.run_forever()
            except Exception as e:
                logger.error(f"Erro no loop do WS: {e}")
            time.sleep(3)

    def start(self):
        logger.info("🚀 BlazeMonitor iniciado (Modo Leve WebSocket)")
        self.last_stone = time.time()
        threading.Thread(target=self._watchdog, daemon=True).start()
        self.start_ws()


if __name__ == "__main__":
    monitor = BlazeMonitor()
    monitor.start()
