import json
import os
import sys
import time
import requests
from datetime import datetime
from collections import defaultdict

# Garante que o diretório atual está no path para permitir imports locais (db.py)
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

from db import get_conn
from dotenv import load_dotenv

# Carregar .env da raiz
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

# ──────────────────────────────────────────────────────────────────────────────
# ESTADO GLOBAL DO SINAL (Independente do Gale Interno das Estratégias)
#
# Chave: (chat_id, min_conf) — SEPARADO por nível de confluência exigida.
# Isso garante que um bot com min_conf=1 e outro com min_conf=2 no mesmo chat
# tenham ciclos de vida completamente independentes e não se interfiram.
#
# Estrutura de cada sessão:
#   {
#     "color":       str,   # Cor aguardada (ex: "Preto")
#     "signal_gale": int,   # Gale do sinal na perspectiva do usuário (G0, G1...)
#     "min_conf":    int,   # Confluência mínima deste ciclo
#     "initial_conf":int,   # Confluência no momento do G0
#     "active_bots": list,  # Robôs que dispararam o G0
#     "bot_details": list,  # Detalhes para cálculo de stake
#     "aborted":     bool,  # True = confluência caiu abaixo do mínimo
#   }
# ──────────────────────────────────────────────────────────────────────────────
signal_sessions: dict = {}   # {(chat_id, target_color): session_dict}

# Delay (segundos) entre uma resolução (WIN/LOSS) e o próximo sinal
SIGNAL_DELAY_AFTER_RESULT = int(os.getenv("SIGNAL_DELAY_AFTER_RESULT", "3"))


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_TOKEN")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code != 200:
            print(f"❌ [Telegram Error] {resp.status_code}: {resp.text}", flush=True)
    except Exception as e:
        print(f"❌ [Telegram Exception] {e}", flush=True)


def save_signal_history(chat_id, color, result, confluences, bots, bot_details=None):
    """Salva o resultado do sinal no banco e atualiza a sessão ativa do grupo."""
    try:
        import json
        conn = get_conn(dict_cursor=False)
        cur = conn.cursor()
        
        # 1. Salva o histórico simples
        cur.execute(
            """
            INSERT INTO signal_history (chat_id, target_color, result, confluences, bot_names)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (str(chat_id), color, result, confluences, ", ".join(bots)),
        )

        # 2. Atualiza a sessão ativa do grupo (PnL, Gales, Wins/Losses)
        cur.execute("SELECT id, gales, wins, losses, pnl, total_signals FROM group_sessions WHERE target_telegram_id = %s AND is_active = TRUE ORDER BY id DESC LIMIT 1", (str(chat_id),))
        session_row = cur.fetchone()

        if session_row and bot_details and len(bot_details) > 0:
            sess_id, gales_str, w, l, current_pnl, total = session_row
            try: gales = json.loads(gales_str) if isinstance(gales_str, str) else (gales_str or {})
            except: gales = {}
            
            cfg = bot_details[0].get("stake_config", {})
            initial = float(cfg.get("initialStake", 2.0))
            mult = float(cfg.get("martingaleMultiplier", 2.0))
            max_gales = int(bot_details[0].get("gales", 2))
            
            payout = 14 if color == "Branco" else 2
            is_win = "WIN" in result
            gale_level = int(result.split("_G")[1]) if is_win else max_gales
                
            total_cost = 0.0
            for g in range(gale_level + 1):
                total_cost += initial * (mult ** g)
                
            pnl = float(current_pnl) - total_cost
            if is_win:
                win_stake = initial * (mult ** gale_level)
                pnl += win_stake * payout
                w += 1
                gale_key = f"G{gale_level}"
                gales[gale_key] = gales.get(gale_key, 0) + 1
            else:
                l += 1
                
            total += 1
            
            cur.execute("""
                UPDATE group_sessions 
                SET wins = %s, losses = %s, pnl = %s, total_signals = %s, gales = %s
                WHERE id = %s
            """, (w, l, pnl, total, json.dumps(gales), sess_id))

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ [DB] Erro ao salvar histórico/sessão: {e}", flush=True)


def update_group_ciclo(chat_id, result_type):
    try:
        import json
        conn = get_conn(dict_cursor=False)
        cur = conn.cursor()
        cur.execute("SELECT id, ciclos FROM group_sessions WHERE target_telegram_id = %s AND is_active = TRUE ORDER BY id DESC LIMIT 1", (str(chat_id),))
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return
            
        sess_id, ciclos_str = row
        ciclos = json.loads(ciclos_str) if ciclos_str else []
        
        if not ciclos:
            ciclos.append({"type": result_type, "count": 1})
        else:
            last_ciclo = ciclos[-1]
            if last_ciclo["type"] == result_type:
                last_ciclo["count"] += 1
            else:
                ciclos.append({"type": result_type, "count": 1})
                
        cur.execute("UPDATE group_sessions SET ciclos = %s WHERE id = %s", (json.dumps(ciclos), sess_id))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ [DB] Erro ao atualizar ciclo: {e}", flush=True)

def get_target_emoji(color):
    if "Branco"   in color: return "⚪"
    if "Vermelho" in color: return "🔴"
    return "⚫"


def get_color_code(roll, color_name):
    n = int(roll)
    if "Vermelho" in str(color_name) or (1 <= n <= 7):  return "V"
    if "Preto"    in str(color_name) or (8 <= n <= 14): return "P"
    return "B" # Branco / 0


def calc_stake(bot_details, signal_gale):
    """Calcula o valor sugerido baseado no gale ATUAL DO SINAL (não da estratégia interna)."""
    first = bot_details[0] if bot_details else {}
    cfg = first.get("stake_config", {})
    if cfg and "initialStake" in cfg:
        initial = float(cfg.get("initialStake", 2.0))
        mult    = float(cfg.get("martingaleMultiplier", 2.0))
        return initial * (mult ** signal_gale)
    return None


def build_signal_msg(color, emoji, confluences, stake):
    """Template de mensagem de sinal (G0)."""
    msg = (
        f"🎯 <b>SINAL CONFIRMADO</b>\n"
        f"🎲 Entrar agora no <b>{color.upper()} {emoji}</b>\n"
        f"🔄 Confluências: <b>{confluences}</b>"
    )
    if stake is not None:
        msg += f"\n💰 Sugestão: <b>R$ {stake:.2f}</b>"
    return msg


def build_gale_msg(color, emoji, signal_gale, confluences, stake):
    """Template de mensagem de Gale (G1+)."""
    msg = (
        f"⚠️ <b>ENTRAR NO {color.upper()} {emoji} AGORA (G{signal_gale})</b>\n"
        f"🔄 Confluências: <b>{confluences}</b>"
    )
    if stake is not None:
        msg += f"\n💰 Sugestão: <b>R$ {stake:.2f}</b>"
    return msg


def calc_sma(values, period):
    if len(values) < period: return None
    return sum(values[-period:]) / period

def calc_ema(values, period):
    if len(values) < period: return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for x in values[period:]:
        ema = (x * k) + (ema * (1 - k))
    return ema

def check_trend(last_rolls, trend_config):
    if not trend_config or not trend_config.get("enabled"):
        return True
    
    acc = 0.0
    acc_values = []
    for r in reversed(last_rolls):
        hp = float(r.get("house_profit", 0))
        acc += hp
        acc_values.append(acc)
    
    p1 = int(trend_config.get("ind1Period", 7))
    p2 = int(trend_config.get("ind2Period", 21))
    
    if len(acc_values) < max(p1, p2):
        return True 
        
    v1 = calc_sma(acc_values, p1) if trend_config.get("ind1Type") == "sma" else calc_ema(acc_values, p1)
    v2 = calc_sma(acc_values, p2) if trend_config.get("ind2Type") == "sma" else calc_ema(acc_values, p2)
    
    if v1 is None or v2 is None:
        return True
        
    return v1 < v2

# ──────────────────────────────────────────────────────────────────────────────
# FUNÇÃO PRINCIPAL
# ──────────────────────────────────────────────────────────────────────────────

def check_user_signals(last_rolls):
    if not last_rolls:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM user_patterns WHERE is_active = TRUE")
    user_bots = cur.fetchall()
    cur.close()
    conn.close()

    if not user_bots:
        return []

    latest_roll = last_rolls[0]
    h_n  = int(latest_roll["roll"])
    is_v = "Vermelho" in latest_roll["color"] or (1 <= h_n <= 7)
    is_p = "Preto"    in latest_roll["color"] or (8 <= h_n <= 14)
    is_b = h_n == 0   or "Branco" in latest_roll["color"]

    had_any_resolution = False

    # ─────────────────────────────────────────────────────────────────────────
    # ETAPA 1 — Calcular quais estratégias batem o padrão AGORA
    #   Resultado: global_counts [(chat_id, target_color)] -> int
    #   Resultado: global_meta   [(chat_id, target_color)] -> list of bot details
    # ─────────────────────────────────────────────────────────────────────────
    global_counts: dict = defaultdict(int)
    global_meta:   dict = defaultdict(lambda: {"bots": [], "bot_details": []})
    
    # Coleta todos os níveis de min_conf configurados por chat (para saber o que monitorar)
    active_thresholds_per_chat: dict = defaultdict(set)

    for bot in user_bots:
        elements      = list(bot["elements"])
        pattern_len   = len(elements)
        entries_limit = bot["max_entries"]
        target_map    = {"B": "Branco", "V": "Vermelho", "P": "Preto"}
        target_color  = target_map.get(bot["target"], bot["target"])
        chat_id       = str(bot["target_telegram_id"])
        bot_min_conf  = int(bot.get("min_confluence") or 1)
        
        active_thresholds_per_chat[chat_id].add(bot_min_conf)
        is_commanding = False

        # Normalização 0 -> B
        for el in elements:
            if el.get("t") == "n" and str(el.get("v")) == "0":
                el["t"] = "c"; el["v"] = "B"

        # Matching
        for step in range(entries_limit):
            pattern_start_idx = step + pattern_len
            if pattern_start_idx > len(last_rolls): break
            history_slice = list(reversed(last_rolls[step:pattern_start_idx]))
            match = True
            for i in range(pattern_len):
                el, h = elements[i], history_slice[i]
                if el["t"] == "c":
                    if get_color_code(h["roll"], h["color"]) != el["v"]: match = False; break
                else:
                    if str(h["roll"]) != str(el["v"]): match = False; break
            if match:
                already_hit = False
                for check in range(step):
                    c_roll = last_rolls[check]
                    c_n = int(c_roll["roll"])
                    c_v, c_p, c_b = (1 <= c_n <= 7), (8 <= c_n <= 14), (c_n == 0)
                    if target_color == "Branco"   and c_b:          already_hit = True; break
                    if target_color == "Vermelho" and (c_v or c_b): already_hit = True; break
                    if target_color == "Preto"    and (c_p or c_b): already_hit = True; break
                if not already_hit:
                    trend_config = bot.get("trend_config")
                    if isinstance(trend_config, str):
                        try: trend_config = json.loads(trend_config)
                        except: trend_config = {}
                    if check_trend(last_rolls, trend_config):
                        is_commanding = True
                    break

        if is_commanding:
            key = (chat_id, target_color)
            global_counts[key] += 1
            global_meta[key]["bots"].append(bot["bot_name"])
            
            stake_config = bot.get("stake_config")
            if isinstance(stake_config, str):
                try: stake_config = json.loads(stake_config)
                except: stake_config = {}
            
            global_meta[key]["bot_details"].append({
                "name": bot["bot_name"], "target": target_color,
                "gales": entries_limit - 1, "stake_config": stake_config or {},
                "group_max_gale": trend_config.get("max_group_gale") if isinstance(trend_config, dict) else None
            })

    # Resolução de conflitos de cores: Se um chat tem mais robôs pedindo Preto do que Vermelho, o Vermelho é zerado para todos os níveis de min_conf daquele chat.
    for chat_id, thresholds in active_thresholds_per_chat.items():
        color_counts = {
            "Vermelho": global_counts.get((chat_id, "Vermelho"), 0),
            "Preto":    global_counts.get((chat_id, "Preto"), 0)
        }
        max_val = max(color_counts.values())
        if max_val > 0:
            winners = [c for c, count in color_counts.items() if count == max_val]
            winner = winners[0] if len(winners) == 1 else None
            for color in color_counts:
                if color != winner:
                    global_counts[(chat_id, color)] = 0

    # ─────────────────────────────────────────────────────────────────────────
    # ETAPA 2 — Verificar resultado das sessões de sinal ativas
    # ─────────────────────────────────────────────────────────────────────────
    resolved_sessions: set = set()  # conjunto de (chat_id, target_color) resolvidos neste tick

    for sess_key, session in list(signal_sessions.items()):
        chat_id       = sess_key[0]
        target_color  = session["color"]
        signal_gale   = session["signal_gale"]
        
        # Atualiza min_conf dinamicamente caso o usuário tenha alterado no front
        current_thresholds = active_thresholds_per_chat.get(chat_id, set())
        if current_thresholds:
            session["min_conf"] = min(current_thresholds)
        
        min_conf      = session["min_conf"]
        aborted       = session["aborted"]

        # Verifica confluência GLOBAL para esta cor (independente do nível da sessão)
        curr_count = global_counts.get((chat_id, target_color), 0)
        curr_meta = global_meta.get((chat_id, target_color))
        
        # Atualização dinâmica: se um novo padrão entrar, herda o novo limite de gales!
        if curr_count > 0 and curr_meta and len(curr_meta["bots"]) > 0:
            session["bot_details"] = curr_meta["bot_details"]
            session["active_bots"] = curr_meta["bots"]

        # ── Resultado do último roll para a cor do sinal ──
        hit_win = (
            (target_color == "Vermelho" and is_v) or
            (target_color == "Preto"    and is_p) or
            (target_color == "Branco"   and is_b)
        )

        # ── Branco Proteção (Ignorar rodada e manter Gale) ──
        # Se a rodada foi Branco e a cor aguardada era Vermelho/Preto, não é loss e nem win.
        # Congelamos o ciclo atual e não aumentamos o gale.
        if is_b and target_color != "Branco":
            print(f"⚪ [Detector] BRANCO ignorado. Mantendo G{signal_gale} | {chat_id} / {target_color}", flush=True)
            continue

        # ── WIN — só envia GREEN se o sinal estava ativo (não abortado) ──
        if hit_win:
            update_group_ciclo(chat_id, "win")
            if not aborted:
                # Sinal foi enviado → WIN real → manda GREEN e encerra o ciclo
                msg = (
                    f"✅ <b>GREEN {target_color.upper()} {get_target_emoji(target_color)}!</b> "
                    f"(G{signal_gale}) — {session.get('current_conf', session['initial_conf'])} confluência(s)"
                )
                send_telegram_message(chat_id, msg)
                save_signal_history(
                    chat_id, target_color, f"WIN_G{signal_gale}",
                    session["initial_conf"], session["active_bots"],
                    session.get("bot_details")
                )
                print(f"✅ [Detector] WIN G{signal_gale} | {chat_id} min_conf={min_conf} / {target_color}", flush=True)
                
                del signal_sessions[sess_key]
                resolved_sessions.add(sess_key)
                had_any_resolution = True
            else:
                # Sinal estava abortado (confluência insuficiente) → WIN ignorado
                # A pedido do usuário: se der WIN (branco) mas o sinal estava abortado (ex: pelo gráfico),
                # a sessão NÃO DEVE ser deletada. Ela deve continuar viva na memória, para que o próximo
                # sinal válido do grupo não comece no G0, mas continue a contagem de gales de onde parou.
                print(f"🔕 [Detector] WIN ignorado (aborted G{signal_gale}) | {chat_id} / {target_color} — Mantendo sessão viva para preservar Gale.", flush=True)
            continue



        # ── LOSS — roll não bateu a cor aguardada ──
        update_group_ciclo(chat_id, "loss")
        if curr_count < min_conf:
            if not aborted:
                session["aborted"] = True
                print(f"🛑 [Detector] ABORT | {chat_id} min_conf={min_conf}/{target_color} — Confluência {curr_count} < {min_conf}", flush=True)
            continue

        # Usuário pediu Gale Ilimitado para a sessão inteira do grupo (chat_id).
        # Se for implementado futuramente na UI um 'group_max_gale', ele respeitará.
        # Caso contrário, o limite individual ('gales') é IGNORADO para que robôs sobrepostos não "zerem" o gale da sessão.
        group_max_gales = [b.get("group_max_gale") for b in session.get("bot_details", [{}]) if b.get("group_max_gale") is not None]
        group_max_gale = max(group_max_gales) if group_max_gales else None

        if group_max_gale is not None:
            if signal_gale >= group_max_gale:
                if not aborted:
                    msg = f"❌ <b>RED {target_color.upper()} {get_target_emoji(target_color)}</b>\nLimite de proteção atingido. Retornando ao G0."
                    send_telegram_message(chat_id, msg)
                    save_signal_history(
                        chat_id, target_color, "LOSS",
                        session["initial_conf"], session["active_bots"],
                        session.get("bot_details")
                    )
                    print(f"❌ [Detector] LOSS (RED) | {chat_id} min_conf={min_conf} / {target_color}", flush=True)
                        
                del signal_sessions[sess_key]
                resolved_sessions.add(sess_key)
                had_any_resolution = True
                continue
        
        # Confluência ativa → avança Gale do sinal
        next_gale = signal_gale + 1
        session["signal_gale"] = next_gale
        session["aborted"] = False  # reativa caso confluência tenha voltado
        session["current_conf"] = curr_count

        stake = calc_stake(session["bot_details"], next_gale)
        emoji = get_target_emoji(target_color)
        msg   = build_gale_msg(target_color, emoji, next_gale, curr_count, stake)
        print(f"⚠️ [Detector] G{next_gale} | {chat_id} min_conf={min_conf} / {target_color}", flush=True)
        send_telegram_message(chat_id, msg)

    # ─────────────────────────────────────────────────────────────────────────
    # DELAY após resolução — evita G1 no mesmo segundo do GREEN
    # ─────────────────────────────────────────────────────────────────────────
    if had_any_resolution and SIGNAL_DELAY_AFTER_RESULT > 0:
        print(f"⏳ [Detector] Aguardando {SIGNAL_DELAY_AFTER_RESULT}s após resolução...", flush=True)
        time.sleep(SIGNAL_DELAY_AFTER_RESULT)

    # ─────────────────────────────────────────────────────────────────────────
    # ETAPA 3 — Disparar novos sinais (G0) 
    # ─────────────────────────────────────────────────────────────────────────
    for (chat_id, target_color), confluences in global_counts.items():
        if confluences <= 0: continue
        
        # Pega a configuração única do grupo para este chat_id
        thresholds = active_thresholds_per_chat.get(chat_id, set())
        if not thresholds: continue
        min_conf = min(thresholds)
        
        if confluences < min_conf:
            continue # Não atingiu o nível exigido para este sinal específico
            
        sess_key = (chat_id, target_color)
        if sess_key in signal_sessions or sess_key in resolved_sessions:
            continue
            
        # ── Novo sinal confirmado para este nível ──
        meta = global_meta[(chat_id, target_color)]

        signal_sessions[sess_key] = {
            "color":        target_color,
            "signal_gale":  0,
            "min_conf":     min_conf,
            "initial_conf": confluences,
            "current_conf": confluences,
            "active_bots":  meta["bots"],
            "bot_details":  meta["bot_details"],
            "aborted":      False,
        }

        stake = calc_stake(meta["bot_details"], 0)
        emoji = get_target_emoji(target_color)
        msg   = build_signal_msg(target_color, emoji, confluences, stake)
        print(f"⚠️ [Detector] G0 | {chat_id} min_conf={min_conf} / {target_color} (conf={confluences})", flush=True)
        send_telegram_message(chat_id, msg)

    # ─────────────────────────────────────────────────────────────────────────
    # ETAPA 4 — Exportar estado para a API ler (Sinais Ativos)
    # ─────────────────────────────────────────────────────────────────────────
    try:
        export_data = []
        for (chat_id, min_conf), session in signal_sessions.items():
            export_data.append({
                "chat_id":      chat_id,
                "color":        session["color"],
                "gale":         session["signal_gale"],
                "confluences":  session["initial_conf"],
                "min_conf":     session["min_conf"],
                "bots":         session["active_bots"],
                "bot_details":  session["bot_details"],
                "aborted":      session["aborted"],
            })
        with open("active_sessions.json", "w") as f:
            json.dump(export_data, f)
    except Exception as e:
        print(f"Erro ao salvar active_sessions: {e}", flush=True)

    return []
