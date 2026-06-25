"""
backtester.py — Motor de Walk-Forward Backtesting

Este módulo simula o comportamento da estratégia no passado, passo a passo,
para gerar relatórios realistas de Drawdown, SM dinâmico e Curva de PNL.
"""

import sys
import os
from typing import List, Dict, Any

# Garante que o diretório atual está no path para permitir imports locais (db.py)
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

from db import get_conn

# Configurações da simulação
MAX_ENTRIES_PER_PATTERN = 5
BET_MULTIPLIER = 1.078
WHITE_PAYOUT = 14
COLOR_PAYOUT = 2

def run_walk_forward_backtest(size: int, limit: int = 5000) -> Dict[str, Any]:
    """
    Roda um backtest walk-forward para um padrão de cor específico.
    """
    conn = get_conn()
    cur = conn.cursor()
    # Buscar os últimos X resultados, ordem cronológica
    cur.execute(
        "SELECT id, color, roll, timestamp FROM results "
        "ORDER BY timestamp DESC LIMIT %s",
        (limit,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    
    # Inverter para ficar do mais antigo para o mais novo
    results = rows[::-1]
    
    if len(results) < size:
        return {"error": "Dados insuficientes."}

    wins = 0
    losses = 0
    sm = 0       
    sa = 0       
    cur_loss = 0
    pnl = 0.0
    
    max_drawdown = 0.0
    peak_pnl = 0.0
    
    active_entries = []
    
    timeline = []

    for i in range(size, len(results)):
        window = results[i - size: i]
        current = results[i]

        is_white = current["color"] == "BRANCO"
        
        # Lógica de detecção de sinal
        non_white = [r["color"] for r in window if r["color"] != "BRANCO"]
        if len(non_white) >= size:
            seq_clean = non_white[-size:]
            if len(set(seq_clean)) == 1:
                dominant = seq_clean[0]
                opposite = "PRETO" if dominant == "VERMELHO" else "VERMELHO"
                
                # Novo sinal! Abrir entrada
                active_entries.append({
                    "target": opposite,
                    "entries_left": MAX_ENTRIES_PER_PATTERN,
                    "current_bet": 1.0,
                    "invested": 1.0,
                    "signal_time": current["timestamp"].isoformat()
                })

        # Resolver entradas ativas
        won_this_round = False
        round_pnl = 0.0
        
        survived = []
        for e in active_entries:
            if is_white:
                # Branco = Win
                gain = (e["current_bet"] * WHITE_PAYOUT) - e["invested"]
                round_pnl += gain
                wins += 1
                won_this_round = True
            elif current["color"] == e["target"]:
                # Acertou a cor = Win
                gain = (e["current_bet"] * COLOR_PAYOUT) - e["invested"]
                round_pnl += gain
                wins += 1
                won_this_round = True
            else:
                # Loss
                e["entries_left"] -= 1
                if e["entries_left"] > 0:
                    next_bet = e["current_bet"] * BET_MULTIPLIER
                    e["current_bet"] = next_bet
                    e["invested"] += next_bet
                    survived.append(e)
                else:
                    # Quebrou o limite (Loss total)
                    round_pnl -= e["invested"]
                    losses += 1
                    cur_loss += 1
                    
        if is_white:
            active_entries = []
        else:
            active_entries = survived

        pnl += round_pnl
        
        if won_this_round:
            cur_loss = 0
            
        sm = max(sm, cur_loss)
        sa = cur_loss
        
        # Calcular Drawdown
        if pnl > peak_pnl:
            peak_pnl = pnl
        drawdown = peak_pnl - pnl
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            
        # Salvar momento na linha do tempo
        timeline.append({
            "timestamp": current["timestamp"].isoformat(),
            "pnl": round(pnl, 2),
            "drawdown": round(drawdown, 2),
            "sm": sm,
            "sa": sa,
            "active_signals": len(active_entries)
        })

    total = wins + losses
    win_rate = round((wins / total * 100) if total > 0 else 0, 2)

    return {
        "summary": {
            "size": size,
            "total_rounds": len(results),
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "final_pnl": round(pnl, 2),
            "max_drawdown": round(max_drawdown, 2),
            "sm_historico": sm,
            "sa_final": sa
        },
        "timeline": timeline
    }
