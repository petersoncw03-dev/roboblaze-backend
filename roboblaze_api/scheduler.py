"""
scheduler.py — Agendador de análises do RoboBlaze Intelligence System

Fica rodando em background e dispara o detector de sinais:
- A cada nova pedra detectada, verifica os robôs dos usuários
"""

import sys
import os
import time
import threading
from datetime import datetime
from multiprocessing import Process

# Garante que o diretório atual está no path para permitir imports locais (db.py)
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

from db import get_conn
from detector import check_user_signals
from pattern_factory import auto_refresh_strategies

# Controle de última pedra processada
_last_processed_id = None
_refresh_process = None


def log(msg):
    print(msg, flush=True)


def get_latest_results(limit=500):
    """Busca as últimas N pedras do banco para análise de padrão."""
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, color, roll, timestamp, COALESCE(house_profit, 0) as house_profit FROM results ORDER BY timestamp DESC LIMIT %s", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        log(f"❌ [SCHEDULER] Erro ao buscar pedras: {e}")
        return []


def run_refresh_job():
    try:
        auto_refresh_strategies()
    except Exception as e:
        log(f"❌ [Fábrica] Erro no recálculo: {e}")



# Evento para sinalizar parada do scheduler
_stop_event = threading.Event()

def run_scheduler():
    global _last_processed_id
    log("⏰ [SaaS] Vigia iniciado. Monitorando novos sinais...")

    while not _stop_event.is_set():
        try:
            last_rolls = get_latest_results(500)
            
            if last_rolls:
                latest_id = last_rolls[0]["id"]
                
                if latest_id != _last_processed_id:
                    _last_processed_id = latest_id
                    log(f"🎯 [SaaS] Nova pedra ({latest_id}). Recalculando + verificando sinais...")
                    
                    global _refresh_process
                    # 1) Recalcula padrões das configs salvas (em processo separado para não travar a API)
                    if _refresh_process is None or not _refresh_process.is_alive():
                        _refresh_process = Process(target=run_refresh_job, daemon=True)
                        _refresh_process.start()
                    else:
                        log("⚠️ [Fábrica] Recálculo anterior ainda rodando. Ignorando para evitar sobrecarga...")

                    # 2) Verifica sinais dos padrões atualizados
                    try:
                        check_user_signals(last_rolls)
                    except Exception as e:
                        log(f"❌ [SaaS] Erro ao verificar sinais: {e}")

            time.sleep(5) 

        except Exception as e:
            log(f"❌ [SaaS] Erro no vigia: {e}")
            time.sleep(10)

def start_scheduler_thread():
    """Inicia o scheduler em uma thread separada."""
    _stop_event.clear()
    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()
    log("🚀 [SaaS] Vigia em background ativado.")
    log("🏭 [Fábrica] Recálculo ativado a cada rodada.")

def stop_scheduler():
    """Para o scheduler graciosamente."""
    _stop_event.set()

