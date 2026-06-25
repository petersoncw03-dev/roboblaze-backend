import sys
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

from db import get_conn

def wipe_all_bots():
    print("Iniciando limpeza da base de dados...")
    conn = get_conn(dict_cursor=False)
    cur = conn.cursor()
    
    # Deletando os robôs
    cur.execute("DELETE FROM user_patterns")
    # Deletando as configurações (receitas)
    cur.execute("DELETE FROM strategy_configs")
    # Deletando as sessões de grupo do telegram
    cur.execute("DELETE FROM group_sessions")
    
    conn.commit()
    print("✅ 🧹 Todos os robôs e estratégias antigas foram apagados com sucesso!")
    cur.close()
    conn.close()

if __name__ == "__main__":
    wipe_all_bots()
