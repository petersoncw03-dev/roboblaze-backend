"""
analyzer.py — Motor de análise do RoboBlaze Intelligence System

Responsável por:
- Calcular WinRate, SM, SA, PNL para os 40 combos (10 tamanhos × 4 períodos)
- Detectar confluências (2+ padrões disparando juntos)
- Gerenciar recordes: MAX_ATIVOS e SM_RECORDE
- Registrar sinais ao vivo em live_signals
"""

import sys
import os
import json
from datetime import datetime, timedelta

# Garante que o diretório atual está no path para permitir imports locais (db.py)
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

from db import get_conn

# ─── Configurações dos 40 Combos ──────────────────────────────────────────────
PATTERN_SIZES = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
PERIODS_HOURS = [12, 24, 48, 60]
MAX_ENTRIES_PER_PATTERN = 5  # Quantas entradas por padrão ao entrar num sinal

# Multiplicador de aposta (Martingale leve)
BET_MULTIPLIER = 1.078
WHITE_PAYOUT = 14
COLOR_PAYOUT = 2


def log(msg):
    print(msg, flush=True)


# ─── Busca de Dados ────────────────────────────────────────────────────────────
def fetch_results(hours: int):
    """Busca pedras do banco para o período solicitado."""
    conn = get_conn()
    cur = conn.cursor()
    since = datetime.now() - timedelta(hours=hours)
    cur.execute(
        "SELECT id, color, roll, timestamp FROM results "
        "WHERE timestamp >= %s ORDER BY timestamp ASC",
        (since,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


# ─── Cálculo de Padrões de Cores ──────────────────────────────────────────────
def analyze_color_pattern(results: list, size: int) -> dict:
    """
    Analisa um padrão de cores de tamanho N.
    Retorna wins, losses, SM, SA, PNL e lista de sinais ativos.
    """
    wins = 0
    losses = 0
    sm = 0       # Sequência máxima de perdas no histórico
    sa = 0       # Sequência atual de perda
    cur_loss = 0
    pnl = 0.0
    active_entries = []  # Entradas abertas atualmente

    for i in range(size, len(results)):
        window = results[i - size: i]
        current = results[i]

        # Monta a sequência de cores (ignorando brancos para o padrão)
        seq = [r["color"] for r in window]
        is_white = current["color"] == "BRANCO"

        # Detecta se os últimos N não-brancos formam uma sequência pura
        non_white = [r["color"] for r in window if r["color"] != "BRANCO"]
        if len(non_white) < size:
            continue
        seq_clean = non_white[-size:]

        # Sinal: todos iguais (ex: 5 vermelhos seguidos)
        if len(set(seq_clean)) == 1:
            dominant = seq_clean[0]
            opposite = "PRETO" if dominant == "VERMELHO" else "VERMELHO"

            # Abrir entrada apostando na cor oposta
            active_entries.append({
                "target": opposite,
                "entries_left": MAX_ENTRIES_PER_PATTERN,
                "current_bet": 1.0,
                "invested": 1.0,
            })

        # Processar entradas abertas
        won_this_round = False
        if is_white:
            # Branco paga para quem estiver em jogo
            for e in active_entries:
                pnl += (e["current_bet"] * WHITE_PAYOUT) - e["invested"]
                wins += 1
                won_this_round = True
            active_entries = []
        else:
            survived = []
            for e in active_entries:
                if current["color"] == e["target"]:
                    pnl += (e["current_bet"] * COLOR_PAYOUT) - e["invested"]
                    wins += 1
                    won_this_round = True
                else:
                    e["entries_left"] -= 1
                    if e["entries_left"] > 0:
                        next_bet = e["current_bet"] * BET_MULTIPLIER
                        e["current_bet"] = next_bet
                        e["invested"] += next_bet
                        survived.append(e)
                    else:
                        pnl -= e["invested"]
                        losses += 1
                        cur_loss += 1
            active_entries = survived

        if won_this_round:
            cur_loss = 0
        sm = max(sm, cur_loss)

    sa = cur_loss
    total = wins + losses
    win_rate = round((wins / total * 100) if total > 0 else 0, 2)

    return {
        "wins": wins,
        "losses": losses,
        "sm": sm,
        "sa": sa,
        "pnl": round(pnl, 2),
        "win_rate": win_rate,
        "active_count": len(active_entries),
    }


# ─── Análise de Todos os 40 Combos ────────────────────────────────────────────
def run_full_analysis():
    """
    Roda os 40 combos (10 tamanhos × 4 períodos) e salva em pattern_stats.
    Também detecta confluências entre padrões ativos.
    """
    log("🧠 [ANALYZER] Iniciando análise completa dos 40 combos...")

    conn = get_conn()
    cur = conn.cursor()

    active_patterns = {}  # { period: [lista de padrões com sinal ativo] }

    for hours in PERIODS_HOURS:
        results = fetch_results(hours)
        if len(results) < 20:
            log(f"⚠️ [ANALYZER] Dados insuficientes para {hours}h ({len(results)} pedras). Pulando.")
            continue

        active_patterns[hours] = []

        for size in PATTERN_SIZES:
            pattern_id = f"color_{size}_{hours}h"
            stats = analyze_color_pattern(results, size)

            # Salvar / Atualizar em pattern_stats
            cur.execute("""
                INSERT INTO pattern_stats (id, type, size, period_hours, win_rate, wins, losses, sm, sa, pnl, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    win_rate = EXCLUDED.win_rate,
                    wins = EXCLUDED.wins,
                    losses = EXCLUDED.losses,
                    sm = EXCLUDED.sm,
                    sa = EXCLUDED.sa,
                    pnl = EXCLUDED.pnl,
                    updated_at = NOW();
            """, (
                pattern_id, "color", size, hours,
                stats["win_rate"], stats["wins"], stats["losses"],
                stats["sm"], stats["sa"], stats["pnl"]
            ))

            # Se tem sinal ativo, adiciona à lista de confluências
            if stats["active_count"] > 0:
                active_patterns[hours].append({
                    "id": pattern_id,
                    "size": size,
                    "active_count": stats["active_count"],
                    "sa": stats["sa"],
                    "sm": stats["sm"],
                })

        # Detectar e processar confluências para esse período
        _process_confluences(cur, active_patterns[hours], hours)

    conn.commit()
    cur.close()
    conn.close()
    log("✅ [ANALYZER] Análise completa salva no banco.")


# ─── Confluências ──────────────────────────────────────────────────────────────
def _process_confluences(cur, active_list: list, hours: int):
    """
    Detecta confluências (2+ padrões ativos ao mesmo tempo).
    Atualiza recordes de MAX_ATIVOS e SM.
    """
    if len(active_list) < 2:
        return

    strategies_count = len(active_list)
    total_active = sum(p["active_count"] for p in active_list)
    max_sa = max(p["sa"] for p in active_list)

    # ID único para essa confluência
    conf_id = f"conf_{strategies_count}strat_{hours}h"

    # Busca o recorde atual
    cur.execute("SELECT * FROM confluence_records WHERE id = %s", (conf_id,))
    record = cur.fetchone()

    if record is None:
        # Primeira vez que essa confluência aparece
        cur.execute("""
            INSERT INTO confluence_records
            (id, strategies_count, max_entries, period_hours,
             max_ativos_recorde, max_ativos_wins, max_ativos_losses, max_ativos_set_at,
             last_3_occurrences, sm_recorde, sm_recorde_set_at, sa_atual, updated_at)
            VALUES (%s, %s, %s, %s, %s, 0, 0, NOW(), '[]', %s, NOW(), %s, NOW())
        """, (conf_id, strategies_count, total_active, hours, total_active, max_sa, max_sa))
        log(f"🆕 [CONF] Nova confluência registrada: {conf_id}")
        return

    record = dict(record)
    max_ativos = record["max_ativos_recorde"]
    sm_rec = record["sm_recorde"]
    last_3 = record["last_3_occurrences"] if isinstance(record["last_3_occurrences"], list) else []

    updates = {}

    # ── Lógica MAX_ATIVOS ──
    if total_active > max_ativos:
        # Novo recorde! Zera placar W/L
        updates["max_ativos_recorde"] = total_active
        updates["max_ativos_wins"] = 0
        updates["max_ativos_losses"] = 0
        updates["max_ativos_set_at"] = datetime.now()
        updates["last_3_occurrences"] = json.dumps([])
        log(f"🏆 [RECORDE] {conf_id} novo MAX_ATIVOS: {total_active} (anterior: {max_ativos})")

        # Registrar sinal
        _save_signal(cur, conf_id, "record_broken",
                     f"Novo recorde de ativos: {total_active} estratégias simultâneas!",
                     {"conf_id": conf_id, "new_record": total_active, "old_record": max_ativos})

    elif total_active == max_ativos:
        # Atingiu exatamente o recorde — guarda nos últimos 3
        occurrence = {"time": datetime.now().isoformat(), "active": total_active}
        last_3 = ([occurrence] + last_3)[:3]
        updates["last_3_occurrences"] = json.dumps(last_3)

    # ── Lógica SM_RECORDE ──
    if max_sa > sm_rec:
        updates["sm_recorde"] = max_sa
        updates["sm_recorde_set_at"] = datetime.now()
        log(f"📊 [SM] {conf_id} novo SM_RECORDE: {max_sa} (anterior: {sm_rec})")

    elif max_sa >= sm_rec and sm_rec > 0:
        # SA atual atingiu o SM recorde → ALERTA
        _save_signal(cur, conf_id, "sm_alert",
                     f"⚠️ SA={max_sa} atingiu o SM Recorde histórico={sm_rec}!",
                     {"conf_id": conf_id, "sa": max_sa, "sm_recorde": sm_rec})
        log(f"🚨 [ALERTA] {conf_id} SA={max_sa} tocou SM_RECORDE={sm_rec}!")

    updates["sa_atual"] = max_sa
    updates["updated_at"] = datetime.now()

    if updates:
        set_clause = ", ".join(f"{k} = %s" for k in updates.keys())
        cur.execute(
            f"UPDATE confluence_records SET {set_clause} WHERE id = %s",
            list(updates.values()) + [conf_id]
        )


def _save_signal(cur, pattern_id: str, signal_type: str, message: str, data: dict):
    """Salva um sinal ao vivo na tabela live_signals."""
    cur.execute("""
        INSERT INTO live_signals (pattern_id, type, message, data_json, created_at)
        VALUES (%s, %s, %s, %s, NOW())
    """, (pattern_id, signal_type, message, json.dumps(data)))


# ─── Ponto de Entrada para Testes ─────────────────────────────────────────────
if __name__ == "__main__":
    run_full_analysis()
