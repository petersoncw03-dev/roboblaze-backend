import sys
import os
import json
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Carregar .env da raiz
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

# Adiciona o caminho da pasta atual ao sys.path para o Docker encontrar o db.py, analyzer.py, etc.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from db import get_conn, setup_tables
from scheduler import start_scheduler_thread, stop_scheduler
from backtester import run_walk_forward_backtest


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 [API] RoboBlaze Intelligence API iniciando...", flush=True)
    setup_tables()
    start_scheduler_thread()
    yield
    print("🛑 [API] Encerrando...", flush=True)
    stop_scheduler()


app = FastAPI(
    title="RoboBlaze Intelligence API",
    version="2.2.4",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Health ───────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "version": "2.2.4", "timestamp": datetime.now().isoformat()}


# ─── Resultados ───────────────────────────────────────────────────────────────
@app.get("/results")
def get_results(limit: int = Query(200, ge=1, le=10000)):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, color, roll, timestamp, total_bets, total_payout, house_profit "
        "FROM results ORDER BY timestamp DESC LIMIT %s", (limit,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return {"data": rows, "total": len(rows)}


@app.get("/results/period")
def get_results_period(hours: int = Query(24, ge=1, le=800)):
    conn = get_conn()
    cur = conn.cursor()
    since = datetime.now() - timedelta(hours=hours)
    cur.execute(
        "SELECT id, color, roll, timestamp, total_bets, total_payout, house_profit "
        "FROM results WHERE timestamp >= %s ORDER BY timestamp ASC", (since,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return {"data": rows, "total": len(rows), "period_hours": hours}


# ─── Padrões ──────────────────────────────────────────────────────────────────
@app.get("/patterns/all")
def get_all_patterns(type: str = Query("color")):
    """Retorna todos os combos calculados. O site só renderiza."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM pattern_stats WHERE type = %s ORDER BY period_hours, size",
        (type,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()

    # Organizar por período para facilitar o frontend
    grouped = {}
    for row in rows:
        period = row["period_hours"]
        if period not in grouped:
            grouped[period] = []
        grouped[period].append(row)

    return {"patterns": grouped, "total": len(rows)}


@app.get("/patterns")
def get_pattern(size: int = Query(5), period: int = Query(24), type: str = Query("color")):
    conn = get_conn()
    cur = conn.cursor()
    pattern_id = f"{type}_{size}_{period}h"
    cur.execute("SELECT * FROM pattern_stats WHERE id = %s", (pattern_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return {"error": "Padrão não encontrado ainda. Aguarde a próxima análise."}
    return dict(row)


# ─── Confluências + Recordes ──────────────────────────────────────────────────
@app.get("/confluences")
def get_confluences(period: int = Query(None)):
    """Retorna confluências com recordes MAX_ATIVOS e SM."""
    conn = get_conn()
    cur = conn.cursor()
    if period:
        cur.execute(
            "SELECT * FROM confluence_records WHERE period_hours = %s ORDER BY strategies_count",
            (period,)
        )
    else:
        cur.execute("SELECT * FROM confluence_records ORDER BY period_hours, strategies_count")
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return {"confluences": rows, "total": len(rows)}


# ─── Sinais Ao Vivo ───────────────────────────────────────────────────────────
@app.get("/signals")
def get_signals(limit: int = Query(20, ge=1, le=100)):
    """Retorna os últimos N sinais para o site mostrar ao vivo."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM live_signals ORDER BY created_at DESC LIMIT %s", (limit,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return {"signals": rows, "total": len(rows)}


# ─── Financeiro + SMA/EMA ─────────────────────────────────────────────────────
@app.get("/financial")
def get_financial(
    hours: int = Query(24, ge=1, le=72),
    sma_period: int = Query(10),
    ema_period: int = Query(10)
):
    conn = get_conn()
    cur = conn.cursor()
    since = datetime.now() - timedelta(hours=hours)
    cur.execute(
        "SELECT timestamp, house_profit, total_bets, total_payout "
        "FROM results WHERE timestamp >= %s ORDER BY timestamp ASC", (since,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()

    cumulative = 0
    ema_val = None
    k = 2 / (ema_period + 1)
    chart_data = []

    for i, row in enumerate(rows):
        profit = float(row["house_profit"] or 0)
        cumulative += profit

        start = max(0, i - sma_period + 1)
        sma = sum(float(r["house_profit"] or 0) for r in rows[start:i + 1]) / (i - start + 1)

        ema_val = profit if ema_val is None else (profit * k) + (ema_val * (1 - k))

        ts = row["timestamp"]
        chart_data.append({
            "time": ts.strftime("%H:%M") if hasattr(ts, "strftime") else str(ts)[:16],
            "lucro": round(profit, 2),
            "acumulado": round(cumulative, 2),
            "sma": round(sma, 2),
            "ema": round(ema_val, 2),
            "total_bets": float(row["total_bets"] or 0),
        })

    total_profit = sum(float(r["house_profit"] or 0) for r in rows)
    total_bets = sum(float(r["total_bets"] or 0) for r in rows)

    return {
        "chart": chart_data,
        "summary": {
            "total_profit": round(total_profit, 2),
            "total_bets": round(total_bets, 2),
            "house_edge_pct": round((total_profit / total_bets * 100) if total_bets > 0 else 0, 2),
            "rounds": len(rows)
        }
    }


# ─── Stats Rápidas ────────────────────────────────────────────────────────────
@app.get("/stats")
def get_stats(hours: int = Query(24, ge=1, le=72)):
    conn = get_conn()
    cur = conn.cursor()
    since = datetime.now() - timedelta(hours=hours)
    cur.execute(
        "SELECT color, COUNT(*) as total FROM results WHERE timestamp >= %s GROUP BY color",
        (since,)
    )
    rows = cur.fetchall()
    cur.close(); conn.close()

    stats = {"VERMELHO": 0, "PRETO": 0, "BRANCO": 0}
    for row in rows:
        stats[row["color"]] = int(row["total"])
    total = sum(stats.values())

    return {
        "period_hours": hours, "total_rounds": total,
        "vermelho": stats["VERMELHO"], "preto": stats["PRETO"], "branco": stats["BRANCO"],
        "pct_vermelho": round(stats["VERMELHO"] / total * 100, 1) if total > 0 else 0,
        "pct_preto": round(stats["PRETO"] / total * 100, 1) if total > 0 else 0,
        "pct_branco": round(stats["BRANCO"] / total * 100, 1) if total > 0 else 0,
    }


# ─── Analista (Backtest Walk-Forward) ──────────────────────────────────────────
@app.get("/backtest")
def get_backtest(size: int = Query(5, ge=3, le=12), limit: int = Query(5000, ge=100, le=20000)):
    """
    Roda a simulação walk-forward de uma estratégia no histórico.
    """
    return run_walk_forward_backtest(size, limit)

# ─── Gerenciamento de Robôs (SaaS) ───────────────────────────────────────────
import json
from pydantic import BaseModel
from typing import List, Dict, Any, Optional

class UserBotCreate(BaseModel):
    user_id: str
    bot_name: str
    elements: List[Dict[str, Any]]
    target: str
    max_entries: int = 3
    target_telegram_id: Optional[str] = None
    is_active: bool = True
    min_confluence: int = 1
    trend_config: Optional[Dict[str, Any]] = {}

@app.post("/save-pattern")
def save_pattern(bot: UserBotCreate):
    """Cria ou salva um novo robô de monitoramento na VPS."""
    conn = get_conn(dict_cursor=False)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_patterns 
        (user_id, bot_name, elements, target, max_entries, is_active, target_telegram_id, min_confluence, trend_config)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
    """, (
        bot.user_id, bot.bot_name, json.dumps(bot.elements), bot.target,
        bot.max_entries, bot.is_active, bot.target_telegram_id, bot.min_confluence, json.dumps(bot.trend_config)
    ))
    bot_id = cur.fetchone()[0]
    conn.commit()
    cur.close(); conn.close()
    if bot.target_telegram_id:
        check_and_update_session(bot.target_telegram_id)
    return {"status": "success", "bot_id": bot_id, "message": "Robô salvo na nuvem com sucesso!"}

@app.get("/user-patterns/{user_id}")
def get_user_patterns(user_id: str):
    """Lista todos os robôs configurados de um usuário."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM user_patterns WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return rows

@app.put("/user-patterns/{bot_id}/toggle")
def toggle_pattern(bot_id: int):
    """Liga ou desliga a automação de um robô específico."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE user_patterns SET is_active = NOT is_active WHERE id = %s RETURNING is_active", (bot_id,))
    result = cur.fetchone()
    if not result:
        cur.close(); conn.close()
        return {"status": "error", "message": "Robô não encontrado."}
    
    new_status = result['is_active']
    conn.commit()
    cur.close(); conn.close()
    return {"status": "success", "is_active": new_status, "message": f"Robô {'ligado' if new_status else 'desligado'} com sucesso."}

@app.delete("/user-patterns/{bot_id}")
def delete_pattern(bot_id: int):
    """Exclui um robô permanentemente do sistema."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM user_patterns WHERE id = %s", (bot_id,))
    conn.commit()
    cur.close(); conn.close()
    return {"status": "success", "message": "Robô deletado."}

@app.delete("/user-patterns/all/{user_id}")
def delete_all_patterns(user_id: str):
    """Deleta TODOS os robôs de um usuário."""
    conn = get_conn(dict_cursor=False)
    cur = conn.cursor()
    
    cur.execute("SELECT DISTINCT target_telegram_id FROM strategy_configs WHERE user_id = %s", (user_id,))
    telegram_ids = [r[0] for r in cur.fetchall()]
    
    cur.execute("DELETE FROM user_patterns WHERE user_id = %s", (user_id,))
    cur.execute("DELETE FROM strategy_configs WHERE user_id = %s", (user_id,))
    
    if telegram_ids:
        cur.execute("UPDATE group_sessions SET is_active = FALSE, session_end = NOW() WHERE target_telegram_id = ANY(%s)", (telegram_ids,))
        
    conn.commit()
    cur.close()
    conn.close()
    return {"status": "success", "deleted": True}

@app.get("/user-patterns/{user_id}/active-count")
def get_active_count(user_id: str):
    """Conta robôs ativos."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as total FROM user_patterns WHERE user_id = %s AND is_active = TRUE", (user_id,))
    total = dict(cur.fetchone())['total']
    cur.execute("SELECT COUNT(*) as auto_count FROM user_patterns WHERE user_id = %s AND auto_generated = TRUE AND is_active = TRUE", (user_id,))
    auto = dict(cur.fetchone())['auto_count']
    cur.close(); conn.close()
    return {"total": total, "auto_generated": auto, "manual": total - auto}

@app.get("/active-sessions")
def get_active_sessions():
    """Retorna os sinais ativos atualmente sendo enviados no Telegram."""
    try:
        import os, json
        if os.path.exists("active_sessions.json"):
            with open("active_sessions.json", "r") as f:
                data = json.load(f)
            return {"sessions": data}
        return {"sessions": []}
    except Exception as e:
        return {"sessions": [], "error": str(e)}

@app.put("/user-patterns/stop-all/{user_id}")
def stop_all_patterns(user_id: str):
    """Desliga TODOS os robôs de um usuário (sem deletar)."""
    conn = get_conn(dict_cursor=False)
    cur = conn.cursor()
    cur.execute("UPDATE user_patterns SET is_active = FALSE WHERE user_id = %s RETURNING target_telegram_id", (user_id,))
    chat_ids = set(row[0] for row in cur.fetchall() if row[0])
    
    # Também pausa configs
    cur.execute("UPDATE strategy_configs SET auto_refresh = FALSE WHERE user_id = %s RETURNING target_telegram_id", (user_id,))
    chat_ids.update(row[0] for row in cur.fetchall() if row[0])
    
    conn.commit(); cur.close(); conn.close()
    
    for cid in chat_ids:
        check_and_update_session(cid)
        
    return {"status": "success", "stopped": len(chat_ids)}

# ─── Fábrica IA (Estratégias = Filtros) ───────────────────────────────────────

class StrategyConfigCreate(BaseModel):
    user_id: str
    target_telegram_id: str
    filters: dict
    name: str = "Estratégia IA"
    min_confluence: int = 1

@app.post("/save-strategy-config")
def save_strategy_config(config: StrategyConfigCreate):
    conn = get_conn(dict_cursor=False)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO strategy_configs (user_id, target_telegram_id, filters, auto_refresh, name, min_confluence)
        VALUES (%s, %s, %s, TRUE, %s, %s) RETURNING id
    """, (config.user_id, config.target_telegram_id, json.dumps(config.filters), config.name, config.min_confluence))
    config_id = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    check_and_update_session(config.target_telegram_id)
    return {"status": "success", "config_id": config_id}

@app.get("/strategy-configs/{user_id}")
def get_strategy_configs(user_id: str):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM strategy_configs WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
        configs = [dict(r) for r in cur.fetchall()]
        
        # Otimização: contar todos os padrões de uma vez em vez de fazer um loop no banco
        cur.execute("""
            SELECT bot_name, COUNT(*) as cnt 
            FROM user_patterns 
            WHERE user_id = %s AND auto_generated = TRUE 
            GROUP BY bot_name
        """, (user_id,))
        pattern_counts = {r['bot_name']: r['cnt'] for r in cur.fetchall()}
        
        for cfg in configs:
            config_id = cfg['id']
            # Somar padrões que começam com Auto_{id}_
            cfg['active_patterns'] = sum(count for name, count in pattern_counts.items() if name.startswith(f"Auto_{config_id}_"))
            
            # Serializar timestamps
            for k in ['created_at', 'last_refresh']:
                if cfg.get(k): 
                    cfg[k] = cfg[k].isoformat() + "Z"
            if isinstance(cfg.get('filters'), str):
                cfg['filters'] = json.loads(cfg['filters'])
        
        return {"configs": configs}
    finally:
        cur.close()
        conn.close()

def check_and_update_session(chat_id):
    if not chat_id: return
    conn = get_conn(dict_cursor=False)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM user_patterns WHERE target_telegram_id = %s AND is_active = TRUE", (chat_id,))
    bots_active = cur.fetchone()[0] > 0
    cur.execute("SELECT COUNT(*) FROM strategy_configs WHERE target_telegram_id = %s AND auto_refresh = TRUE", (chat_id,))
    strategies_active = cur.fetchone()[0] > 0
    
    is_group_active = bots_active or strategies_active
    
    cur.execute("SELECT id FROM group_sessions WHERE target_telegram_id = %s AND is_active = TRUE", (chat_id,))
    active_session = cur.fetchone()
    
    if is_group_active and not active_session:
        cur.execute("INSERT INTO group_sessions (target_telegram_id) VALUES (%s)", (chat_id,))
    elif not is_group_active and active_session:
        cur.execute("UPDATE group_sessions SET is_active = FALSE, session_end = NOW() WHERE id = %s", (active_session[0],))
    
    conn.commit(); cur.close(); conn.close()

@app.delete("/strategy-configs/{config_id}")
def delete_strategy_config(config_id: int):
    conn = get_conn(dict_cursor=False)
    cur = conn.cursor()
    cur.execute("SELECT target_telegram_id FROM strategy_configs WHERE id = %s", (config_id,))
    row = cur.fetchone()
    chat_id = row[0] if row else None
    
    cur.execute("DELETE FROM user_patterns WHERE bot_name LIKE %s", (f"Auto_{config_id}_%",))
    cur.execute("DELETE FROM strategy_configs WHERE id = %s", (config_id,))
    conn.commit(); cur.close(); conn.close()
    
    if chat_id: check_and_update_session(chat_id)
    return {"status": "success"}

@app.patch("/strategy-configs/{config_id}/toggle")
def toggle_strategy_config(config_id: int):
    conn = get_conn(dict_cursor=False)
    cur = conn.cursor()
    cur.execute("UPDATE strategy_configs SET auto_refresh = NOT auto_refresh WHERE id = %s RETURNING target_telegram_id", (config_id,))
    row = cur.fetchone()
    chat_id = row[0] if row else None
    conn.commit(); cur.close(); conn.close()
    
    if chat_id: check_and_update_session(chat_id)
    return {"status": "success"}

class StrategyConfigUpdate(BaseModel):
    name: str
    target_telegram_id: str

@app.patch("/strategy-configs/{config_id}/update")
def update_strategy_config(config_id: int, data: StrategyConfigUpdate):
    conn = get_conn(dict_cursor=False)
    cur = conn.cursor()
    cur.execute("SELECT target_telegram_id FROM strategy_configs WHERE id = %s", (config_id,))
    row = cur.fetchone()
    old_chat_id = row[0] if row else None
    
    cur.execute(
        "UPDATE strategy_configs SET name = %s, target_telegram_id = %s WHERE id = %s",
        (data.name, data.target_telegram_id, config_id)
    )
    conn.commit(); cur.close(); conn.close()
    
    if old_chat_id and old_chat_id != data.target_telegram_id:
        check_and_update_session(old_chat_id)
    check_and_update_session(data.target_telegram_id)
    
    return {"status": "success"}

@app.get("/group-sessions")
def get_group_sessions():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM group_sessions ORDER BY session_start DESC LIMIT 100")
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        for k in ['session_start', 'session_end']:
            if r.get(k): r[k] = r[k].isoformat() + "Z"
        if isinstance(r.get('gales'), str):
            try: r['gales'] = json.loads(r['gales'])
            except: r['gales'] = {}
    cur.close(); conn.close()
    return {"sessions": rows}

@app.post("/group-sessions/{telegram_id}/reset")
def reset_group_session(telegram_id: str):
    conn = get_conn(dict_cursor=False)
    cur = conn.cursor()
    try:
        cur.execute("UPDATE group_sessions SET is_active = FALSE, session_end = NOW() WHERE target_telegram_id = %s AND is_active = TRUE", (telegram_id,))
        cur.execute("INSERT INTO group_sessions (target_telegram_id) VALUES (%s)", (telegram_id,))
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()




@app.patch("/strategy-configs/group/{telegram_id}/max-gale")
def update_group_max_gale(telegram_id: str, payload: dict):
    max_gale = payload.get("max_group_gale")
    conn = get_conn(dict_cursor=False)
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, trend_config FROM user_patterns WHERE target_telegram_id = %s", (telegram_id,))
        rows = cur.fetchall()
        for pat_id, tc_str in rows:
            try: tc = json.loads(tc_str) if isinstance(tc_str, str) else (tc_str or {})
            except: tc = {}
            tc["max_group_gale"] = max_gale
            cur.execute("UPDATE user_patterns SET trend_config = %s WHERE id = %s", (json.dumps(tc), pat_id))
        
        cur.execute("SELECT id, filters FROM strategy_configs WHERE target_telegram_id = %s", (telegram_id,))
        rows = cur.fetchall()
        for cfg_id, f_str in rows:
            try: f_json = json.loads(f_str) if isinstance(f_str, str) else (f_str or {})
            except: f_json = {}
            f_json["max_group_gale"] = max_gale
            cur.execute("UPDATE strategy_configs SET filters = %s WHERE id = %s", (json.dumps(f_json), cfg_id))

        conn.commit()
        return {"status": "success", "telegram_id": telegram_id, "max_group_gale": max_gale}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

@app.patch("/strategy-configs/group/{telegram_id}/confluence")
def update_group_confluence(telegram_id: str, payload: dict):
    min_conf = payload.get("min_confluence", 1)
    conn = get_conn(dict_cursor=False)
    cur = conn.cursor()
    # Atualiza todas as configs pai
    cur.execute(
        "UPDATE strategy_configs SET min_confluence = %s WHERE target_telegram_id = %s",
        (min_conf, telegram_id)
    )
    # Atualiza todos os padrões/robôs filhos rodando na VPS
    cur.execute(
        "UPDATE user_patterns SET min_confluence = %s WHERE target_telegram_id = %s",
        (min_conf, telegram_id)
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"status": "success", "telegram_id": telegram_id, "min_confluence": min_conf}

# ─── Assertividade Real ───────────────────────────────────────────────────────

@app.get("/signal-stats")
def get_signal_stats(hours: int = Query(24, ge=1, le=168)):
    """Retorna o placar real dos sinais enviados no Telegram."""
    conn = get_conn()
    cur = conn.cursor()
    since = datetime.now() - timedelta(hours=hours)
    
    cur.execute("""
        SELECT result, COUNT(*) as count 
        FROM signal_history 
        WHERE created_at >= %s 
        GROUP BY result
    """, (since,))
    
    rows = cur.fetchall()
    cur.close(); conn.close()
    
    stats = {
        "total": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0,
        "g0": 0,
        "g1": 0,
        "g2": 0,
        "others": 0
    }
    
    for row in rows:
        res = row['result']
        count = int(row['count'])
        stats["total"] += count
        
        if res.startswith("WIN"):
            stats["wins"] += count
            if "G0" in res: stats["g0"] += count
            elif "G1" in res: stats["g1"] += count
            elif "G2" in res: stats["g2"] += count
            else: stats["others"] += count
        else:
            stats["losses"] += count
            
    if stats["total"] > 0:
        stats["win_rate"] = round((stats["wins"] / stats["total"]) * 100, 1)
        
    return stats

# ─── Automação Local ──────────────────────────────────────────────────────────
from pydantic import BaseModel
from typing import Optional, List

class BotConfig(BaseModel):
    token: str
    base_amount: float
    stop_win: float
    stop_loss: float
    max_gale: int
    use_soros: bool

class BotSignal(BaseModel):
    target: str
    pattern_id: str

# Em memória (Uso single-tenant para o dono)
bot_current_config = None
bot_signal_queue = []

@app.post("/bot/config")
def update_bot_config(config: BotConfig):
    global bot_current_config
    bot_current_config = config.dict()
    return {"status": "success", "config": bot_current_config}

@app.post("/bot/signal")
def add_bot_signal(signal: BotSignal):
    global bot_signal_queue
    bot_signal_queue.append(signal.dict())
    return {"status": "success"}

@app.get("/bot/state")
def get_bot_state():
    global bot_signal_queue
    signals = list(bot_signal_queue)
    bot_signal_queue.clear()
    return {
        "config": bot_current_config,
        "signals": signals
    }



class TrendConfigUpdate(BaseModel):
    useTrendFilter: bool
    ind1Type: str = "sma"
    ind1Period: int = 7
    ind2Type: str = "ema"
    ind2Period: int = 21

@app.patch("/strategy-configs/group/{telegram_id}/trend")
def update_group_trend(telegram_id: str, trend_data: TrendConfigUpdate):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, filters FROM strategy_configs WHERE target_telegram_id = %s", (telegram_id,))
        configs = cur.fetchall()
        for r in configs:
            cid = r['id']
            f = r['filters']
            if isinstance(f, str): f = json.loads(f)
            f['useTrendFilter'] = trend_data.useTrendFilter
            if trend_data.useTrendFilter:
                f['ind1Type'] = trend_data.ind1Type
                f['ind1Period'] = trend_data.ind1Period
                f['ind2Type'] = trend_data.ind2Type
                f['ind2Period'] = trend_data.ind2Period
            cur.execute("UPDATE strategy_configs SET filters = %s WHERE id = %s", (json.dumps(f), cid))
            
        cur.execute("SELECT id, trend_config FROM user_patterns WHERE target_telegram_id = %s", (telegram_id,))
        patterns = cur.fetchall()
        for r in patterns:
            pid = r['id']
            tc_str = r['trend_config']
            try: tc = json.loads(tc_str) if isinstance(tc_str, str) else (tc_str or {})
            except: tc = {}
            
            tc['enabled'] = trend_data.useTrendFilter
            if trend_data.useTrendFilter:
                tc['ind1Type'] = trend_data.ind1Type
                tc['ind1Period'] = trend_data.ind1Period
                tc['ind2Type'] = trend_data.ind2Type
                tc['ind2Period'] = trend_data.ind2Period
            
            cur.execute("UPDATE user_patterns SET trend_config = %s WHERE id = %s", (json.dumps(tc), pid))
            
        conn.commit()
        return {"status": "success", "updated_count": len(configs)}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

