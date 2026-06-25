"""
pattern_factory.py — Engine de descoberta de padrões (Python)
Porta da lógica do frontend IA Analista para rodar na VPS automaticamente.
"""
import sys
import os
import json
from datetime import datetime

# Garante que o diretório atual está no path para permitir imports locais (db.py)
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

from db import get_conn

def log(msg):
    print(msg, flush=True)

def get_color_code(roll_val, color_name):
    n = int(roll_val)
    if "Vermelho" in str(color_name) or (1 <= n <= 7): return "V"
    if "Preto" in str(color_name) or (8 <= n <= 14): return "P"
    return "B"

def evaluate_hit(roll_obj, target):
    n = int(roll_obj['roll'])
    is_b = n == 0 or "Branco" in str(roll_obj.get('color',''))
    is_v = "Vermelho" in str(roll_obj.get('color','')) or (1 <= n <= 7)
    is_p = "Preto" in str(roll_obj.get('color','')) or (8 <= n <= 14)
    if target == 'Branco' and is_b: return True
    if target == 'Vermelho' and is_v: return True
    if target == 'Preto' and is_p: return True
    return False

def build_elements(history, i, total_len, pat_type):
    elements = []
    if pat_type == 'ONLY_COLORS':
        for p in range(total_len):
            elements.append({'t':'c','v': get_color_code(history[i+p]['roll'], history[i+p].get('color',''))})
    elif pat_type == 'ONLY_NUMBERS':
        for p in range(total_len):
            elements.append({'t':'n','v': str(history[i+p]['roll'])})
    elif pat_type == 'COLORS_1_NUM':
        for p in range(total_len-1):
            elements.append({'t':'c','v': get_color_code(history[i+p]['roll'], history[i+p].get('color',''))})
        elements.append({'t':'n','v': str(history[i+total_len-1]['roll'])})
    elif pat_type == '1_NUM_COLORS':
        elements.append({'t':'n','v': str(history[i]['roll'])})
        for p in range(1, total_len):
            elements.append({'t':'c','v': get_color_code(history[i+p]['roll'], history[i+p].get('color',''))})
    elif pat_type == 'COLORS_2_NUM':
        for p in range(total_len-2):
            elements.append({'t':'c','v': get_color_code(history[i+p]['roll'], history[i+p].get('color',''))})
        elements.append({'t':'n','v': str(history[i+total_len-2]['roll'])})
        elements.append({'t':'n','v': str(history[i+total_len-1]['roll'])})
    elif pat_type == '2_NUM_COLORS':
        elements.append({'t':'n','v': str(history[i]['roll'])})
        elements.append({'t':'n','v': str(history[i+1]['roll'])})
        for p in range(2, total_len):
            elements.append({'t':'c','v': get_color_code(history[i+p]['roll'], history[i+p].get('color',''))})
    return elements

def get_sizes_for_type(pat_type, selected_size=0):
    if selected_size > 0: return [selected_size]
    sizes = {
        'ONLY_COLORS': [2,3,4,5,6], 'ONLY_NUMBERS': [1,2,3],
        'COLORS_1_NUM': [3,4,5], '1_NUM_COLORS': [3,4,5],
        'COLORS_2_NUM': [4,5], '2_NUM_COLORS': [4,5],
    }
    return sizes.get(pat_type, [3])

def run_discovery(filters, history=None):
    """Roda a engine de descoberta. Otimizada para performance."""
    period = filters.get('periodHours', 24)
    pat_type = filters.get('patternType', 'ONLY_COLORS')
    entries_limit = filters.get('entriesLimit', 3)
    target_focus = filters.get('targetFocus', 'Branco')
    min_triggers = filters.get('minTriggers', 5)
    min_wr = filters.get('minWinRate', 90)
    max_sa = filters.get('maxSa', 2)
    min_sa = filters.get('minSaFilter', 0)
    selected_size = filters.get('selectedSize', 0)

    rows = history
    if rows is None:
        conn = get_conn()
        cur = conn.cursor()
        limit = period * 120
        cur.execute("SELECT id, color, roll, timestamp FROM results ORDER BY timestamp DESC LIMIT %s", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse() # Colocar em ordem cronológica (velho -> novo) para o loop
        cur.close(); conn.close()

    if len(rows) < 10: return []

    # Pré-calcular códigos de cor e tipos de acerto para acelerar o loop
    for r in rows:
        r['_c'] = get_color_code(r['roll'], r.get('color',''))
        r['_is_b'] = r['roll'] == 0 or "Branco" in str(r.get('color',''))
        r['_is_v'] = "Vermelho" in str(r.get('color','')) or (1 <= r['roll'] <= 7)
        r['_is_p'] = "Preto" in str(r.get('color','')) or (8 <= r['roll'] <= 14)

    types_to_test = ['ONLY_COLORS','ONLY_NUMBERS','COLORS_1_NUM','COLORS_2_NUM','1_NUM_COLORS','2_NUM_COLORS'] if pat_type == 'TODOS' else [pat_type]
    targets = ['Vermelho','Preto'] if target_focus == 'Ambos' else [target_focus]

    pattern_map = {}

    use_mixed_mining = filters.get('useMixedMining', False)

    for target in targets:
        if use_mixed_mining:
            sizes = [1, 2, 3, 4, 5, 6, 7]
            for total_len in sizes:
                for i in range(len(rows) - entries_limit - total_len):
                    if total_len <= 4:
                        num_variations = 1 << total_len
                        for mask in range(num_variations):
                            has_zero_as_num = False
                            for p in range(total_len):
                                if (mask & (1 << p)) == 0:
                                    elements.append(f"c{rows[i+p]['_c']}")
                                else:
                                    if str(rows[i+p]['roll']) == '0':
                                        has_zero_as_num = True
                                    elements.append(f"n{rows[i+p]['roll']}")
                            
                            if has_zero_as_num and pat_type != 'ONLY_NUMBERS':
                                continue

                            key = f"{target}:MIXED:{'|'.join(elements)}"
                            if key not in pattern_map:
                                pattern_map[key] = {'triggers':0,'wins':0,'current_sa':0,'max_sa':0,'ptype':'MIXED','target':target}
                            
                            pobj = pattern_map[key]
                            pobj['triggers'] += 1
                            
                            hit = False
                            for w in range(1, entries_limit+1):
                                r_nxt = rows[i + total_len - 1 + w]
                                if target == 'Branco' and r_nxt['_is_b']: hit = True; break
                                if target == 'Vermelho' and r_nxt['_is_v']: hit = True; break
                                if target == 'Preto' and r_nxt['_is_p']: hit = True; break
                            
                            if hit:
                                pobj['wins'] += 1
                                pobj['current_sa'] = 0
                            else:
                                pobj['current_sa'] += 1
                                if pobj['current_sa'] > pobj['max_sa']: pobj['max_sa'] = pobj['current_sa']
                    else:
                        types_to_test_mixed = ['ONLY_COLORS', 'COLORS_1_NUM', '1_NUM_COLORS'] if pat_type == 'TODOS' else [pat_type]
                        for ptype in types_to_test_mixed:
                            elements = []
                            if ptype == 'ONLY_COLORS':
                                for p in range(total_len): elements.append(f"c{rows[i+p]['_c']}")
                            elif ptype == 'ONLY_NUMBERS':
                                for p in range(total_len): elements.append(f"n{rows[i+p]['roll']}")
                            elif ptype == 'COLORS_1_NUM':
                                for p in range(total_len-1): elements.append(f"c{rows[i+p]['_c']}")
                                elements.append(f"n{rows[i+total_len-1]['roll']}")
                            elif ptype == '1_NUM_COLORS':
                                elements.append(f"n{rows[i]['roll']}")
                                for p in range(1, total_len): elements.append(f"c{rows[i+p]['_c']}")
                            elif ptype == 'COLORS_2_NUM' and total_len >= 4:
                                for p in range(total_len-2): elements.append(f"c{rows[i+p]['_c']}")
                                elements.append(f"n{rows[i+total_len-2]['roll']}")
                                elements.append(f"n{rows[i+total_len-1]['roll']}")
                            elif ptype == '2_NUM_COLORS' and total_len >= 4:
                                elements.append(f"n{rows[i]['roll']}")
                                elements.append(f"n{rows[i+1]['roll']}")
                                for p in range(2, total_len): elements.append(f"c{rows[i+p]['_c']}")

                            if not elements: continue

                            if pat_type != 'ONLY_NUMBERS' and any(e == 'n0' for e in elements):
                                continue

                            key = f"{target}:{ptype}:{'|'.join(elements)}"
                            if key not in pattern_map:
                                pattern_map[key] = {'triggers':0,'wins':0,'current_sa':0,'max_sa':0,'ptype':ptype,'target':target}
                            
                            pobj = pattern_map[key]
                            pobj['triggers'] += 1
                            
                            hit = False
                            for w in range(1, entries_limit+1):
                                r_nxt = rows[i + total_len - 1 + w]
                                if target == 'Branco' and r_nxt['_is_b']: hit = True; break
                                if target == 'Vermelho' and r_nxt['_is_v']: hit = True; break
                                if target == 'Preto' and r_nxt['_is_p']: hit = True; break
                            
                            if hit:
                                pobj['wins'] += 1
                                pobj['current_sa'] = 0
                            else:
                                pobj['current_sa'] += 1
                                if pobj['current_sa'] > pobj['max_sa']: pobj['max_sa'] = pobj['current_sa']
        else:
            for ptype in types_to_test:
                sizes = get_sizes_for_type(ptype, selected_size)
                for total_len in sizes:
                    for i in range(len(rows) - entries_limit - total_len):
                        elements = []
                        if ptype == 'ONLY_COLORS':
                            for p in range(total_len): elements.append(f"c{rows[i+p]['_c']}")
                        elif ptype == 'ONLY_NUMBERS':
                            for p in range(total_len): elements.append(f"n{rows[i+p]['roll']}")
                        elif ptype == 'COLORS_1_NUM':
                            for p in range(total_len-1): elements.append(f"c{rows[i+p]['_c']}")
                            elements.append(f"n{rows[i+total_len-1]['roll']}")
                        elif ptype == '1_NUM_COLORS':
                            elements.append(f"n{rows[i]['roll']}")
                            for p in range(1, total_len): elements.append(f"c{rows[i+p]['_c']}")
                        elif ptype == 'COLORS_2_NUM':
                            for p in range(total_len-2): elements.append(f"c{rows[i+p]['_c']}")
                            elements.append(f"n{rows[i+total_len-2]['roll']}")
                            elements.append(f"n{rows[i+total_len-1]['roll']}")
                        elif ptype == '2_NUM_COLORS':
                            elements.append(f"n{rows[i]['roll']}")
                            elements.append(f"n{rows[i+1]['roll']}")
                            for p in range(2, total_len): elements.append(f"c{rows[i+p]['_c']}")

                        if not elements: continue

                        if pat_type != 'ONLY_NUMBERS' and any(e == 'n0' for e in elements):
                            continue

                        key = f"{target}:{ptype}:{'|'.join(elements)}"
                        if key not in pattern_map:
                            pattern_map[key] = {'triggers':0,'wins':0,'current_sa':0,'max_sa':0,'ptype':ptype,'target':target}
                        
                        pobj = pattern_map[key]
                        pobj['triggers'] += 1
                        
                        hit = False
                        for w in range(1, entries_limit+1):
                            r_nxt = rows[i + total_len - 1 + w]
                            if target == 'Branco' and r_nxt['_is_b']: hit = True; break
                            if target == 'Vermelho' and r_nxt['_is_v']: hit = True; break
                            if target == 'Preto' and r_nxt['_is_p']: hit = True; break
                        
                        if hit:
                            pobj['wins'] += 1
                            pobj['current_sa'] = 0
                        else:
                            pobj['current_sa'] += 1
                            if pobj['current_sa'] > pobj['max_sa']: pobj['max_sa'] = pobj['current_sa']

    results = []
    # Re-converter strings de elements de volta para objetos para o DB
    for k, v in pattern_map.items():
        wr = (v['wins'] / max(v['triggers'],1)) * 100
        if v['triggers'] >= min_triggers and wr >= min_wr and v['max_sa'] <= max_sa and v['current_sa'] >= min_sa:
            # Reconstruir lista de elements objetos
            parts = k.split(':')[-1].split('|')
            elem_objs = []
            for p in parts: elem_objs.append({'t': p[0], 'v': p[1:] if p[0]=='c' else int(p[1:])})

            results.append({
                'elements': elem_objs, 'target': v['target'],
                'type': v['ptype'], 'win_rate': round(wr,1),
                'triggers': v['triggers'], 'wins': v['wins']
            })

    results.sort(key=lambda x: x['win_rate'], reverse=True)
    return results

def auto_refresh_strategies():
    """Busca configs com auto_refresh=True e recalcula os padrões."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM strategy_configs WHERE auto_refresh = TRUE")
    configs = [dict(r) for r in cur.fetchall()]
    
    # Buscar histórico máximo necessário uma única vez para otimizar
    max_period = 24
    if configs:
        max_period = max([ (cfg['filters'].get('periodHours', 24) if isinstance(cfg['filters'], dict) else json.loads(cfg['filters']).get('periodHours', 24)) for cfg in configs ] + [24])
    
    cur.execute("SELECT id, color, roll, timestamp FROM results ORDER BY timestamp DESC LIMIT %s", (max_period * 120,))
    history_rows = [dict(r) for r in cur.fetchall()]
    history_rows.reverse() # Ordem cronológica
    cur.close(); conn.close()

    if not configs: return

    target_map = {'Branco':'B','Vermelho':'V','Preto':'P'}

    for cfg in configs:
        config_id = cfg['id']
        user_id = cfg['user_id']
        telegram_id = cfg['target_telegram_id']
        filters = cfg['filters'] if isinstance(cfg['filters'], dict) else json.loads(cfg['filters'])
        entries_limit = filters.get('entriesLimit', 3)
        period = filters.get('periodHours', 24)
        stake_config = {
            "initialStake": filters.get('initialStake', 2.0),
            "martingaleMultiplier": filters.get('martingaleMultiplier', 2.0 if filters.get('targetFocus') != 'Branco' else 1.078)
        }

        log(f"🏭 [Fábrica #{config_id}] Recalculando...")

        min_confluence = cfg.get('min_confluence', 1)

        try:
            # Passar sub-set do histórico se necessário, ou usar tudo
            sub_history = history_rows[-(period * 120):] if len(history_rows) > (period * 120) else history_rows
            patterns = run_discovery(filters, history=sub_history)
            log(f"🏭 [Fábrica #{config_id}] {len(patterns)} padrões descobertos.")

            conn2 = get_conn(dict_cursor=False)
            cur2 = conn2.cursor()
            cur2.execute("DELETE FROM user_patterns WHERE bot_name LIKE %s", (f"Auto_{config_id}_%",))

            trend_config = {
                "enabled": filters.get("useTrendFilter", False),
                "ind1Type": filters.get("ind1Type", "sma"),
                "ind1Period": filters.get("ind1Period", 7),
                "ind2Type": filters.get("ind2Type", "ema"),
                "ind2Period": filters.get("ind2Period", 21)
            }

            saved = 0
            for pat in patterns[:50]:
                t = target_map.get(pat['target'], pat['target'])
                name = f"Auto_{config_id}_{saved+1}"
                cur2.execute("""
                    INSERT INTO user_patterns (user_id, bot_name, elements, target, max_entries, is_active, target_telegram_id, auto_generated, stake_config, min_confluence, trend_config)
                    VALUES (%s, %s, %s, %s, %s, TRUE, %s, TRUE, %s, %s, %s)
                """, (user_id, name, json.dumps(pat['elements']), t, entries_limit, telegram_id, json.dumps(stake_config), min_confluence, json.dumps(trend_config)))
                saved += 1

            cur2.execute("UPDATE strategy_configs SET last_refresh = NOW() WHERE id = %s", (config_id,))
            conn2.commit(); cur2.close(); conn2.close()
            log(f"✅ [Fábrica #{config_id}] {saved} robôs atualizados.")

        except Exception as e:
            log(f"❌ [Fábrica #{config_id}] Erro: {e}")
