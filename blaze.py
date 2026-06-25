import gspread
from google.oauth2.service_account import Credentials
import sys
import os
from dotenv import load_dotenv

# Carregar variáveis do .env na raiz
root_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(root_env)

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
import time
from datetime import datetime, timedelta, timezone
import websocket
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

CREDENTIALS_FILE = 'credentials.json'
SPREADSHEET_NAME = 'Resultados Blaze'
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

def connect_sheets():
    try:
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
        client = gspread.authorize(creds)
        sheet_id = "1tt5KfQWrGdmfzGWPp8sv0jzxoN6LltDdN4wlL46TZ2E"
        return client.open_by_key(sheet_id).sheet1
    except Exception as e:
        print(f"⚠️ Erro Sheets: {e}")
        return None

def format_time(date_str):
    try:
        utc_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        brazil_time = utc_time - timedelta(hours=3)
        return brazil_time.strftime("%H:%M:%S")
    except:
        return datetime.now().strftime("%H:%M:%S")

class BlazeMonitor:
    def __init__(self):
        self.seen_ids = set()
        
        # O sistema agora usa exclusivamente PostgreSQL via VPS
        # para garantir latência ultra baixa (< 100ms).
        print("✅ Inicializando Monitor (Google Sheets desativado, apenas PostgreSQL)")
                
        self.ws = None
        self.ping_thread = None



    def on_message(self, ws, message):
        if message.startswith("0"):
            print("📡 Conectado ao servidor Socket.io da Blaze.")
            ws.send("40") # Conectar ao namespace padrão
        elif message.startswith("40"):
            import os
            token = os.getenv("BLAZE_TOKEN")
            if token:
                print("🔑 Enviando Payload de Autenticação Vip (Token)...")
                ws.send(f'42["cmd",{{"id":"authenticate","payload":{{"token":"{token}"}}}}]')
                ws.send(f'42["cmd",{{"id":"trackTime","payload":{{"token":"{token}"}}}}]')
            
            print("🚀 Enviando inscrição EXCLUSIVA para salas de Resultado (Foco em HFT)...")
            ws.send('420["cmd",{"id":"subscribe","payload":{"room":"double_room_1"}}]')
            ws.send('421["cmd",{"id":"subscribe","payload":{"room":"double"}}]')
            ws.send('422["cmd",{"id":"subscribe","payload":{"room":"roulette"}}]')
            # REMOVIDO: live-bets causava flood de milhares de mensagens por segundo, travando o Event Loop em 8s.
            print("✅ Monitoramento HFT ativo. Aguardando resultados ao vivo sem lag...")
        elif message.startswith("42"):
            try:
                json_str = message.split("[", 1)[1]
                json_str = "[" + json_str
                data = json.loads(json_str)

                if len(data) >= 2 and isinstance(data[1], dict):
                    event_name = data[1].get("id")
                    payload = data[1].get("payload", {})

                    if event_name in ["double.update", "roulette.update", "double.tick"]:
                        status = payload.get("status")
                        # A IA MATOU A CHARADA: 'rolling' acontece 7 segundos antes de 'complete' (durante a animação)
                        if status in ["rolling", "complete"]:
                            r_id = str(payload.get("id"))
                            color = payload.get("color")
                            roll = payload.get("roll")
                            
                            # Só processa se já tiver o resultado e se for inédito
                            if r_id and r_id not in self.seen_ids and color is not None and roll is not None:
                                cor_str = {1: "🔴 Vermelho", 2: "⚫ Preto", 0: "⚪ Branco"}.get(color, f"? ({color})")
                                ts = format_time(payload.get("created_at"))

                                new_data = [r_id, cor_str, roll, ts]
                                self.seen_ids.add(r_id)
                                print(f"\n💎 NOVO REGISTRO (WebSocket): {cor_str} {roll} | {ts}")



                                # Google Sheets foi desativado para reduzir latência. O sistema agora usa apenas PostgreSQL.

                                # --- NOVA LÓGICA VPS: SALVAR NO DB E CHECAR SINAIS ---
                                try:
                                    # Importamos aqui para evitar erro de path caso rode isolado
                                    import sys
                                    import os
                                    project_root = os.path.dirname(os.path.abspath(__file__))
                                    if project_root not in sys.path:
                                        sys.path.append(project_root)
                                    
                                    from roboblaze_api.db import get_conn
                                    from roboblaze_api.detector import check_user_signals

                                    # 1. Salvar no PostgreSQL
                                    conn = get_conn(dict_cursor=False)
                                    cur = conn.cursor()
                                    # Traduzindo cor para o formato do banco
                                    db_color = "BRANCO" if color == 0 else "VERMELHO" if color == 1 else "PRETO"
                                    
                                    # Preservar o timestamp oficial UTC para inserção perfeita no banco timezone-aware
                                    try:
                                        created_at = payload.get("created_at")
                                        utc = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                                        br_time = utc
                                    except Exception:
                                        br_time = datetime.now(timezone.utc)
                                    
                                    cur.execute("""
                                        INSERT INTO results (id, color, roll, timestamp)
                                        VALUES (%s, %s, %s, %s)
                                        ON CONFLICT (id) DO NOTHING
                                    """, (r_id, db_color, roll, br_time))
                                    
                                    # Dispara o NOTIFY se a linha foi realmente inserida
                                    if cur.rowcount > 0:
                                        payload_str = json.dumps({
                                            "id": r_id,
                                            "color": db_color,
                                            "roll": roll,
                                            "timestamp": br_time.isoformat()
                                        })
                                        cur.execute(f"NOTIFY nova_pedra, '{payload_str}'")

                                    conn.commit()

                                    # 2. Buscar histórico recente para o detector
                                    cur.execute("SELECT roll, color FROM results ORDER BY timestamp DESC LIMIT 50")
                                    rows = cur.fetchall()
                                    last_rolls = [{"roll": r[0], "color": r[1]} for r in rows]
                                    
                                    cur.close(); conn.close()

                                    # 3. Rodar Motor de Sinais
                                    signals = check_user_signals(last_rolls)
                                    if signals:
                                        print(f"🎯 {len(signals)} SINAIS DETECTADOS NA VPS!")
                                        for s in signals:
                                            print(f"   > {s['bot_name']} | ENTRADA {s['step']}")

                                except Exception as e:
                                    print(f"⚠️ Erro ao processar DB/Sinais: {e}")
            except Exception as e:
                pass

import websocket
import json
import time
import threading
from datetime import datetime
import ssl
import os
import requests

class BlazeMonitor:
    def __init__(self):
        self.seen_ids = set()
        self.ws = None
        self.access_token = os.getenv("BLAZE_ACCESS_TOKEN")
        self.refresh_token = os.getenv("BLAZE_REFRESH_TOKEN")
        self.token_lock = threading.Lock()
        
        # Modo de Proxy (Lido do .env)
        self.proxy_host = os.getenv("PROXY_HOST")
        self.proxy_port = os.getenv("PROXY_PORT")
        self.proxy_user = os.getenv("PROXY_USER")
        self.proxy_pass = os.getenv("PROXY_PASS")

    def auto_refresh_token(self):
        """Atualiza o token de acesso usando o refresh token a cada 11 horas."""
        while True:
            time.sleep(11 * 3600)  # Renova a cada 11h
            with self.token_lock:
                if not self.refresh_token:
                    print("⚠️ Nenhum BLAZE_REFRESH_TOKEN configurado. Ignorando renovação.")
                    continue
                try:
                    print("🔄 Solicitando novo Access Token para a Blaze...")
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Content-Type": "application/json"
                    }
                    payload = {"refresh_token": self.refresh_token}
                    # O endpoint exato pode variar. Essa é a estrutura padrão.
                    resp = requests.put("https://blaze.bet.br/api/auth/token", json=payload, headers=headers, timeout=10)
                    
                    if resp.status_code in [200, 201]:
                        data = resp.json()
                        if "access_token" in data:
                            self.access_token = data["access_token"]
                            print("✅ Token renovado com sucesso!")
                    else:
                        print(f"❌ Falha ao renovar token. Status: {resp.status_code}")
                except Exception as e:
                    print(f"❌ Erro de rede ao renovar token: {e}")

    def on_message(self, ws, message):
        if message == "2":
            ws.send("3")
        elif message == "3":
            pass
        elif message.startswith("0"):
            print("📡 Conectado ao servidor Socket.io da Blaze.")
            ws.send("40")
        elif message.startswith("40"):
            with self.token_lock:
                if self.access_token:
                    print("🔑 Enviando Payload de Autenticação Vip (Token)...", flush=True)
                    ws.send(f'42["cmd",{{"id":"authenticate","payload":{{"token":"{self.access_token}"}}}}]')
                    ws.send(f'42["cmd",{{"id":"trackTime","payload":{{"token":"{self.access_token}"}}}}]')
            
            print("🚀 Enviando inscrição EXCLUSIVA para salas de Resultado (Foco em HFT)...", flush=True)
            ws.send('420["cmd",{"id":"subscribe","payload":{"room":"double_room_1"}}]')
            print("✅ Monitoramento HFT ativo. Aguardando resultados ao vivo sem lag...", flush=True)
            
        elif message.startswith("42"):
            try:
                json_str = message.split("[", 1)[1]
                json_str = "[" + json_str
                data = json.loads(json_str)

                if len(data) >= 2 and isinstance(data[1], dict):
                    event_name = data[1].get("id")
                    payload = data[1].get("payload", {})

                    if event_name in ["double.update", "roulette.update", "double.tick"]:
                        status = payload.get("status")
                        if status in ["rolling", "complete"]:
                            r_id = str(payload.get("id"))
                            color = payload.get("color")
                            roll = payload.get("roll")
                            
                            if r_id and r_id not in self.seen_ids and color is not None and roll is not None:
                                cor_str = {1: "🔴 Vermelho", 2: "⚫ Preto", 0: "⚪ Branco"}.get(color, f"? ({color})")
                                ts = payload.get("created_at")

                                self.seen_ids.add(r_id)
                                print(f"\n💎 NOVO REGISTRO (WebSocket): {cor_str} {roll} | {ts}", flush=True)

                                try:
                                    import sys
                                    project_root = os.path.dirname(os.path.abspath(__file__))
                                    if project_root not in sys.path:
                                        sys.path.append(project_root)
                                    
                                    from roboblaze_api.db import get_conn
                                    from roboblaze_api.detector import check_user_signals

                                    conn = get_conn(dict_cursor=False)
                                    cur = conn.cursor()
                                    db_color = "BRANCO" if color == 0 else "VERMELHO" if color == 1 else "PRETO"
                                    
                                    # Preservar o timestamp oficial UTC para inserção perfeita no banco timezone-aware
                                    try:
                                        created_at = payload.get("created_at")
                                        utc = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                                        br_time = utc
                                    except Exception:
                                        br_time = datetime.now(timezone.utc)
                                    
                                    cur.execute("""
                                        INSERT INTO results (id, color, roll, timestamp)
                                        VALUES (%s, %s, %s, %s)
                                        ON CONFLICT (id) DO NOTHING
                                    """, (r_id, db_color, roll, br_time))
                                    
                                    if cur.rowcount > 0:
                                        payload_str = json.dumps({
                                            "id": r_id,
                                            "color": db_color,
                                            "roll": roll,
                                            "timestamp": br_time.isoformat()
                                        })
                                        cur.execute(f"NOTIFY nova_pedra, '{payload_str}'")

                                    conn.commit()

                                    cur.execute("SELECT roll, color FROM results ORDER BY timestamp DESC LIMIT 50")
                                    rows = cur.fetchall()
                                    last_rolls = [{"roll": r[0], "color": r[1]} for r in rows]
                                    
                                    cur.close(); conn.close()

                                    check_user_signals(last_rolls)
                                except Exception as e:
                                    print(f"⚠️ Erro ao processar DB/Sinais: {e}", flush=True)
            except Exception as e:
                pass

    def on_error(self, ws, error):
        print(f"❌ Erro na conexão WebSocket: {error}", flush=True)

    def on_close(self, ws, close_status_code, close_msg):
        print("⚠️ Conexão WebSocket encerrada. Tentando reconectar em 10 segundos...", flush=True)
        time.sleep(10)
        self.start()

    def on_open(self, ws):
        print("🌐 Conexão WebSocket estabelecida.", flush=True)
        try:
            import socket
            if ws.sock and ws.sock.sock:
                ws.sock.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                print("⚡ TCP_NODELAY ativado (Latência Otimizada).", flush=True)
        except Exception as e:
            pass

        def ping():
            while ws.keep_running:
                time.sleep(25)
                try:
                    ws.send('420["cmd",{"id":"ping","payload":{"uuid":"12345678-1234-1234-1234-123456789012"}}]')
                    ws.send("2")
                except:
                    break
        threading.Thread(target=ping, daemon=True).start()

    def start(self):
        print("🚀 Iniciando Monitor (Modo WebSocket VIP)...", flush=True)
        
        # Inicia a Thread de Auto-Refresh do Token
        threading.Thread(target=self.auto_refresh_token, daemon=True).start()

        headers = {
            "Origin": "https://blaze.bet.br",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        # Bypass de TLS Fingerprint Simples (Ajustando cifras/protocolos)
        ssl_context = ssl.create_default_context()
        ssl_context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
        ssl_context.set_ciphers("DEFAULT@SECLEVEL=1")

        # Configuração de Proxy se existir
        proxy_opts = {}
        if self.proxy_host and self.proxy_port:
            print(f"🌐 Utilizando Proxy Residencial: {self.proxy_host}:{self.proxy_port}", flush=True)
            proxy_opts["http_proxy_host"] = self.proxy_host
            proxy_opts["http_proxy_port"] = self.proxy_port
            if self.proxy_user and self.proxy_pass:
                proxy_opts["http_proxy_auth"] = (self.proxy_user, self.proxy_pass)

        self.ws = websocket.WebSocketApp("wss://api-v2.blaze.bet.br/replication/?EIO=3&transport=websocket",
                                         header=headers,
                                         on_open=self.on_open,
                                         on_message=self.on_message,
                                         on_error=self.on_error,
                                         on_close=self.on_close)

        self.ws.run_forever(sslopt={"context": ssl_context}, **proxy_opts)

if __name__ == "__main__":
    monitor = BlazeMonitor()
    monitor.start()
