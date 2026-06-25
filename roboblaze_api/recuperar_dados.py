import requests
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Ajusta o path para importar o banco
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from roboblaze_api.db import get_conn
except ImportError:
    # Caso esteja rodando de dentro da pasta roboblaze_api
    sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "roboblaze_api"))
    from db import get_conn

# Carrega configurações
load_dotenv()

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

def fetch_page_with_mirror_rotation(page, attempt=1):
    global current_mirror_idx
    
    domain = MIRRORS[current_mirror_idx]
    url = f"https://{domain}/api/singleplayer-originals/originals/roulette_games/recent/history/1?page={page}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    }
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        
        # Se for rate limited (429), rotacionamos para o próximo mirror
        if resp.status_code == 429:
            if attempt <= 10:
                old_domain = domain
                current_mirror_idx = (current_mirror_idx + 1) % len(MIRRORS)
                new_domain = MIRRORS[current_mirror_idx]
                print(f"⚠️ [WARNING] Página {page} retornou HTTP 429 em {old_domain}. Rotacionando para mirror {new_domain} (Tentativa {attempt}/10 em 1.5s)...")
                time.sleep(1.5)
                return fetch_page_with_mirror_rotation(page, attempt + 1)
            else:
                raise Exception(f"HTTP Status 429 (Rate Limit) após 10 rotações de espelho.")
                
        if resp.status_code != 200:
            if attempt <= 5:
                current_mirror_idx = (current_mirror_idx + 1) % len(MIRRORS)
                next_domain = MIRRORS[current_mirror_idx]
                print(f"⚠️ [WARNING] Página {page} retornou HTTP {resp.status_code} em {domain}. Rotacionando para mirror {next_domain} em 1.5s...")
                time.sleep(1.5)
                return fetch_page_with_mirror_rotation(page, attempt + 1)
            else:
                raise Exception(f"HTTP Status {resp.status_code} na página {page}.")
                
        data = resp.json()
        return data.get("records", [])
        
    except Exception as e:
        if attempt <= 5:
            current_mirror_idx = (current_mirror_idx + 1) % len(MIRRORS)
            next_domain = MIRRORS[current_mirror_idx]
            print(f"⚠️ [WARNING] Erro de conexão em {domain}: {e}. Rotacionando para mirror {next_domain} em 1.5s...")
            time.sleep(1.5)
            return fetch_page_with_mirror_rotation(page, attempt + 1)
        else:
            raise e

def recuperar():
    print("="*60)
    print("🚀 [MIGRAÇÃO VPS] INICIANDO RECUPERAÇÃO EM MASSA (ÚLTIMOS 90k GIROS)")
    print("="*60)
    
    try:
        # 1. Conexão com banco
        conn = get_conn(dict_cursor=False)
        cur = conn.cursor()
        
        # 2. Começar com a base 100% limpa para evitar qualquer desvio do passado
        print("🧹 [PROCESSO] Limpando tabela de resultados para alinhamento limpo...")
        cur.execute("TRUNCATE TABLE results;")
        conn.commit()
        print("✅ [SUCESSO] Tabela results limpa.")
        
        total_paginas = 900 # 900 paginas * 100 = 90.000 giros
        total_adicionados = 0
        color_map = {0: "BRANCO", 1: "VERMELHO", 2: "PRETO"}
        
        print(f"🚀 [PROCESSO] Resgatando {total_paginas * 100} giros oficiais da Blaze...")
        
        for page in range(1, total_paginas + 1):
            records = fetch_page_with_mirror_rotation(page)
            
            if not records:
                print(f"ℹ️ [INFO] Página {page} retornou sem giros. Finalizando download mais cedo.")
                break
                
            batch_data = []
            for item in records:
                r_id = str(item.get("id"))
                color_id = item.get("color")
                roll = item.get("roll")
                created_at = item.get("created_at")
                
                db_color = color_map.get(color_id, "DESCONHECIDO")
                
                # Preservar o timestamp oficial UTC para inserção perfeita no banco timezone-aware
                try:
                    utc_str = created_at.replace("Z", "+00:00")
                    utc_dt = datetime.fromisoformat(utc_str)
                    br_time = utc_dt
                except Exception:
                    br_time = datetime.now(timezone.utc)
                
                batch_data.append((r_id, db_color, roll, br_time, 0.0, 0.0, 0.0))
                
            # Inserção em batch eficiente
            insert_query = """
                INSERT INTO results (id, color, roll, timestamp, total_bets, total_payout, house_profit)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING;
            """
            cur.executemany(insert_query, batch_data)
            conn.commit()
            
            total_adicionados += len(records)
            
            if page % 25 == 0 or page == total_paginas:
                print(f"📊 [PROGRESSO] Página {page}/{total_paginas} processada. Total salvo: {total_adicionados} giros.")
                
            # Delay mínimo de 150ms entre requisições para estabilidade
            time.sleep(0.15)
            
        cur.close()
        conn.close()
        print("="*60)
        print(f"🎉 [MIGRAÇÃO CONCLUÍDA] {total_adicionados} giros oficiais gravados em BRT com sucesso!")
        print("="*60)
        
    except Exception as e:
        print(f"❌ [ERRO CRÍTICO] Falha na migração: {e}")

if __name__ == "__main__":
    recuperar()
