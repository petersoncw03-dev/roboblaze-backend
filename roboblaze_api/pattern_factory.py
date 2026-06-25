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

def generate_wildcard_variations(base_elements, max_wildcards=2):
    results = []
    def generate(current, index, wildcards_used):
        if index == len(base_elements):
            results.append(list(current))
            return
        
        el = base_elements[index]
        if el['t'] == 'n' or el['v'] == 'B':
            current.append(el)
            generate(current, index + 1, wildcards_used)
            current.pop()
        else:
            # Sem curinga
            current.append(el)
            generate(current, index + 1, wildcards_used)
            current.pop()
            
            # Com curinga
            if wildcards_used < max_wildcards:
                # Regras: Nunca começar com DUAL ou TRI (index > 0) e Nunca terminar (index < len(base_elements) - 1)
                if index > 0 and index < len(base_elements) - 1:
                    if el['v'] in ['V', 'P']:
                        current.append({'t': 'c', 'v': 'DUAL'})
                        generate(current, index + 1, wildcards_used + 1)
                        current.pop()
                    
                    current.append({'t': 'c', 'v': 'TRI'})
                    generate(current, index + 1, wildcards_used + 1)
                    current.pop()
    generate([], 0, 0)
    return results

def get_sizes_for_type(pat_type, selected_size=0):
    if selected_size > 0: return [selected_size]
    sizes = {
        'ONLY_COLORS': [2,3,4,5,6], 'ONLY_NUMBERS': [1,2,3],
        'COLORS_1_NUM': [3,4,5], '1_NUM_COLORS': [3,4,5],
        'COLORS_2_NUM': [4,5], '2_NUM_COLORS': [4,5],
    }
    return sizes.get(pat_type, [3])

def run_discovery(filters, history=None):
    """Roda a engine de descoberta em ordem cronológica com suporte a Range de Entradas."""
    period = filters.get('periodHours', 24)
    pat_type = filters.get('patternType', 'ONLY_COLORS')
    
    # Suporte legado e novo (entriesLimit vs entriesRange)
    if 'entriesRange' in filters:
        entries_range = filters['entriesRange']
    else:
        limit = filters.get('entriesLimit', 3)
        entries_range = [limit, limit]
        
    min_entries, max_entries = entries_range[0], entries_range[1]
    
    target_focus = filters.get('targetFocus', 'Branco')
    min_triggers = filters.get('minTriggers', 5)
    min_wr = filters.get('minWinRate', 90)
    max_sa = filters.get('maxSa', 2)
    min_sa = filters.get('minSaFilter', 0)
    selected_size = filters.get('selectedSize', 0)
    use_mixed_mining = filters.get('useMixedMining', False)

    rows = history
    if rows is None:
        conn = get_conn()
        cur = conn.cursor()
        limit_rows = period * 120
        cur.execute("SELECT id, color, roll, timestamp FROM results ORDER BY timestamp DESC LIMIT %s", (limit_rows,))
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse() # Colocar em ordem cronológica (velho -> novo)
        cur.close(); conn.close()

    if len(rows) < 10: return []

    for r in rows:
        r['_c'] = get_color_code(r['roll'], r.get('color',''))
        n = int(r['roll'])
        r['_is_b'] = (n == 0) or ("Branco" in str(r.get('color','')))
        r['_is_v'] = "Vermelho" in str(r.get('color','')) or (1 <= n <= 7)
        r['_is_p'] = "Preto" in str(r.get('color','')) or (8 <= n <= 14)

    types_to_test = ['ONLY_COLORS','ONLY_NUMBERS','COLORS_1_NUM','COLORS_2_NUM','1_NUM_COLORS','2_NUM_COLORS'] if pat_type == 'TODOS' else [pat_type]
    targets = ['Vermelho','Preto'] if target_focus == 'Ambos' else [target_focus]

    pattern_state = {}
    active_keys = set()

    sizes_mixed = [1, 2, 3, 4, 5, 6, 7]

    for i in range(len(rows)):
        # Process active entries (Gales)
        keys_to_remove = []
        for key in active_keys:
            state = pattern_state[key]
            any_active = False
            
            # Evaluate Hit
            is_win = False
            tgt = state['target']
            if tgt == 'Branco' and rows[i]['_is_b']: is_win = True
            elif tgt == 'Vermelho' and rows[i]['_is_v']: is_win = True
            elif tgt == 'Preto' and rows[i]['_is_p']: is_win = True

            for e in range(min_entries, max_entries + 1):
                e_state = state['entriesData'][e]
                if e_state['activeEntriesLeft'] > 0:
                    any_active = True
                    if is_win:
                        e_state['wins'] += 1
                        e_state['currentSa'] = 0
                        e_state['activeEntriesLeft'] = 0
                    else:
                        e_state['currentSa'] += 1
                        if e_state['currentSa'] > e_state['sm']:
                            e_state['sm'] = e_state['currentSa']
                        e_state['activeEntriesLeft'] -= 1

            if not any_active:
                keys_to_remove.append(key)
        
        for k in keys_to_remove:
            active_keys.remove(k)

        # Trigger new patterns
        for target in targets:
            if use_mixed_mining:
                for total_len in sizes_mixed:
                    start_idx = i - total_len + 1
                    if start_idx < 0: continue
                    
                    if total_len <= 5:
                        num_variations = 1 << total_len
                        for mask in range(num_variations):
                            elements = []
                            has_zero_as_num = False
                            for p in range(total_len):
                                r_obj = rows[start_idx + p]
                                if (mask & (1 << p)) == 0:
                                    elements.append({'t': 'c', 'v': r_obj['_c']})
                                else:
                                    if str(r_obj['roll']) == '0': has_zero_as_num = True
                                    elements.append({'t': 'n', 'v': str(r_obj['roll'])})
                            
                            if has_zero_as_num and pat_type != 'ONLY_NUMBERS': continue
                            
                            for var_elements in generate_wildcard_variations(elements, 2):
                                key_str = f"{target}:{'|'.join([el['t']+el['v'] for el in var_elements])}"
                                if key_str not in pattern_state:
                                    pattern_state[key_str] = {'type': 'MIXED', 'target': target, 'elements': var_elements, 'entriesData': {}}
                                    for e in range(min_entries, max_entries + 1):
                                        pattern_state[key_str]['entriesData'][e] = {'triggers': 0, 'wins': 0, 'sm': 0, 'currentSa': 0, 'activeEntriesLeft': 0}
                                
                                for e in range(min_entries, max_entries + 1):
                                    pattern_state[key_str]['entriesData'][e]['triggers'] += 1
                                    pattern_state[key_str]['entriesData'][e]['activeEntriesLeft'] = e
                                active_keys.add(key_str)
                    else:
                        types_to_test_mixed = ['ONLY_COLORS', 'COLORS_1_NUM', '1_NUM_COLORS'] if pat_type == 'TODOS' else [pat_type]
                        for ptype in types_to_test_mixed:
                            elements = []
                            if ptype == 'ONLY_COLORS':
                                for p in range(total_len): elements.append({'t':'c', 'v': rows[start_idx+p]['_c']})
                            elif ptype == 'ONLY_NUMBERS':
                                for p in range(total_len): elements.append({'t':'n', 'v': str(rows[start_idx+p]['roll'])})
                            elif ptype == 'COLORS_1_NUM':
                                for p in range(total_len-1): elements.append({'t':'c', 'v': rows[start_idx+p]['_c']})
                                elements.append({'t':'n', 'v': str(rows[i]['roll'])})
                            elif ptype == 'COLORS_2_NUM' and total_len >= 4:
                                for p in range(total_len-2): elements.append({'t':'c', 'v': rows[start_idx+p]['_c']})
                                elements.append({'t':'n', 'v': str(rows[i-1]['roll'])})
                                elements.append({'t':'n', 'v': str(rows[i]['roll'])})
                            elif ptype == '1_NUM_COLORS':
                                elements.append({'t':'n', 'v': str(rows[start_idx]['roll'])})
                                for p in range(1, total_len): elements.append({'t':'c', 'v': rows[start_idx+p]['_c']})
                            elif ptype == '2_NUM_COLORS' and total_len >= 4:
                                elements.append({'t':'n', 'v': str(rows[start_idx]['roll'])})
                                elements.append({'t':'n', 'v': str(rows[start_idx+1]['roll'])})
                                for p in range(2, total_len): elements.append({'t':'c', 'v': rows[start_idx+p]['_c']})

                            if not elements: continue
                            if pat_type != 'ONLY_NUMBERS' and any((el['t']=='n' and el['v']=='0') for el in elements): continue

                            for var_elements in generate_wildcard_variations(elements, 2):
                                key_str = f"{target}:{'|'.join([el['t']+el['v'] for el in var_elements])}"
                                if key_str not in pattern_state:
                                    pattern_state[key_str] = {'type': ptype, 'target': target, 'elements': var_elements, 'entriesData': {}}
                                    for e in range(min_entries, max_entries + 1):
                                        pattern_state[key_str]['entriesData'][e] = {'triggers': 0, 'wins': 0, 'sm': 0, 'currentSa': 0, 'activeEntriesLeft': 0}
                                
                                for e in range(min_entries, max_entries + 1):
                                    pattern_state[key_str]['entriesData'][e]['triggers'] += 1
                                    pattern_state[key_str]['entriesData'][e]['activeEntriesLeft'] = e
                                active_keys.add(key_str)
            else:
                for ptype in types_to_test:
                    sizes = get_sizes_for_type(ptype, selected_size)
                    for total_len in sizes:
                        start_idx = i - total_len + 1
                        if start_idx < 0: continue
                        
                        elements = []
                        if ptype == 'ONLY_COLORS':
                            for p in range(total_len): elements.append({'t':'c', 'v': rows[start_idx+p]['_c']})
                        elif ptype == 'ONLY_NUMBERS':
                            for p in range(total_len): elements.append({'t':'n', 'v': str(rows[start_idx+p]['roll'])})
                        elif ptype == 'COLORS_1_NUM':
                            for p in range(total_len-1): elements.append({'t':'c', 'v': rows[start_idx+p]['_c']})
                            elements.append({'t':'n', 'v': str(rows[i]['roll'])})
                        elif ptype == 'COLORS_2_NUM' and total_len >= 4:
                            for p in range(total_len-2): elements.append({'t':'c', 'v': rows[start_idx+p]['_c']})
                            elements.append({'t':'n', 'v': str(rows[i-1]['roll'])})
                            elements.append({'t':'n', 'v': str(rows[i]['roll'])})
                        elif ptype == '1_NUM_COLORS':
                            elements.append({'t':'n', 'v': str(rows[start_idx]['roll'])})
                            for p in range(1, total_len): elements.append({'t':'c', 'v': rows[start_idx+p]['_c']})
                        elif ptype == '2_NUM_COLORS' and total_len >= 4:
                            elements.append({'t':'n', 'v': str(rows[start_idx]['roll'])})
                            elements.append({'t':'n', 'v': str(rows[start_idx+1]['roll'])})
                            for p in range(2, total_len): elements.append({'t':'c', 'v': rows[start_idx+p]['_c']})

                        if not elements: continue
                        if pat_type != 'ONLY_NUMBERS' and any((el['t']=='n' and el['v']=='0') for el in elements): continue

                        for var_elements in generate_wildcard_variations(elements, 2):
                            key_str = f"{target}:{'|'.join([el['t']+el['v'] for el in var_elements])}"
                            if key_str not in pattern_state:
                                pattern_state[key_str] = {'type': ptype, 'target': target, 'elements': var_elements, 'entriesData': {}}
                                for e in range(min_entries, max_entries + 1):
                                    pattern_state[key_str]['entriesData'][e] = {'triggers': 0, 'wins': 0, 'sm': 0, 'currentSa': 0, 'activeEntriesLeft': 0}
                            
                            for e in range(min_entries, max_entries + 1):
                                pattern_state[key_str]['entriesData'][e]['triggers'] += 1
                                pattern_state[key_str]['entriesData'][e]['activeEntriesLeft'] = e
                            active_keys.add(key_str)

    results = []
    for k, v in pattern_state.items():
        for e in range(min_entries, max_entries + 1):
            e_state = v['entriesData'][e]
            wr = (e_state['wins'] / max(e_state['triggers'], 1)) * 100
            if e_state['triggers'] >= min_triggers and wr >= min_wr and e_state['sm'] <= max_sa and e_state['currentSa'] >= min_sa:
                results.append({
                    'id': f"{k}|ENT_{e}",
                    'entries': e,
                    'elements': v['elements'],
                    'target': v['target'],
                    'type': v['type'],
                    'win_rate': round(wr, 1),
                    'triggers': e_state['triggers'],
                    'wins': e_state['wins'],
                    'sa': e_state['currentSa'],
                    'sm': e_state['sm']
                })

    results.sort(key=lambda x: (x['win_rate'], x['triggers'], -x['sa']), reverse=True)
    return results

import concurrent.futures

def _process_single_config(cfg, history_rows):
    import json
    from db import get_conn
    
    config_id = cfg['id']
    user_id = cfg['user_id']
    telegram_id = cfg['target_telegram_id']
    filters = cfg['filters'] if isinstance(cfg['filters'], dict) else json.loads(cfg['filters'])
    
    period = filters.get('periodHours', 24)
    stake_config = {
        "initialStake": filters.get('initialStake', 2.0),
        "martingaleMultiplier": filters.get('martingaleMultiplier', 2.0 if filters.get('targetFocus') != 'Branco' else 1.078)
    }

    log(f"🏭 [Fábrica #{config_id}] Recalculando em thread isolada...")
    min_confluence = cfg.get('min_confluence', 1)

    try:
        sub_history = history_rows[-(period * 120):] if len(history_rows) > (period * 120) else history_rows
        patterns = run_discovery(filters, history=sub_history)
        log(f"🏭 [Fábrica #{config_id}] {len(patterns)} padrões descobertos.")

        conn2 = get_conn(dict_cursor=False)
        cur2 = conn2.cursor()
        
        cur2.execute("SELECT id FROM strategy_configs WHERE id = %s", (config_id,))
        if not cur2.fetchone():
            conn2.close()
            return f"Config {config_id} ignorada (não existe mais)."
            
        cur2.execute("DELETE FROM user_patterns WHERE bot_name LIKE %s", (f"Auto_{config_id}_%",))

        trend_config = {
            "enabled": filters.get("useTrendFilter", False),
            "ind1Type": filters.get("ind1Type", "sma"),
            "ind1Period": filters.get("ind1Period", 7),
            "ind2Type": filters.get("ind2Type", "ema"),
            "ind2Period": filters.get("ind2Period", 21),
            "max_group_gale": filters.get("max_group_gale")
        }

        saved = 0
        target_map = {'Branco':'B','Vermelho':'V','Preto':'P'}
        for pat in patterns[:200]:
            t = target_map.get(pat['target'], pat['target'])
            name = f"Auto_{config_id}_{saved+1}"
            entries_limit = pat['entries']
            
            cur2.execute("""
                INSERT INTO user_patterns (user_id, bot_name, elements, target, max_entries, is_active, target_telegram_id, auto_generated, stake_config, min_confluence, trend_config)
                VALUES (%s, %s, %s, %s, %s, TRUE, %s, TRUE, %s, %s, %s)
            """, (user_id, name, json.dumps(pat['elements']), t, entries_limit, telegram_id, json.dumps(stake_config), min_confluence, json.dumps(trend_config)))
            saved += 1

        cur2.execute("UPDATE strategy_configs SET last_refresh = NOW() WHERE id = %s", (config_id,))
        conn2.commit(); cur2.close(); conn2.close()
        log(f"✅ [Fábrica #{config_id}] {saved} robôs atualizados via Multiprocessing.")
        return f"Config {config_id} finalizada com sucesso."

    except Exception as e:
        log(f"❌ [Fábrica #{config_id}] Erro: {e}")
        return f"Erro na {config_id}: {e}"

def auto_refresh_strategies():
    """Busca configs com auto_refresh=True e recalcula os padrões (usando Multiprocessing)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM strategy_configs WHERE auto_refresh = TRUE")
    configs = [dict(r) for r in cur.fetchall()]
    
    max_period = 24
    if configs:
        max_period = max([ (cfg['filters'].get('periodHours', 24) if isinstance(cfg['filters'], dict) else json.loads(cfg['filters']).get('periodHours', 24)) for cfg in configs ] + [24])
    
    cur.execute("SELECT id, color, roll, timestamp FROM results ORDER BY timestamp DESC LIMIT %s", (max_period * 120,))
    history_rows = [dict(r) for r in cur.fetchall()]
    history_rows.reverse()
    cur.close(); conn.close()

    if not configs: return

    max_workers = min(12, len(configs)) if len(configs) > 0 else 1
    log(f"🚀 [SaaS] Iniciando mineração paralela V12 com {max_workers} núcleos para {len(configs)} configs.")

    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_process_single_config, cfg, history_rows) for cfg in configs]
        for future in concurrent.futures.as_completed(futures):
            try:
                res = future.result()
            except Exception as e:
                log(f"❌ Erro fatal em núcleo de mineração: {e}")

