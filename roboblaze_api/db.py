import psycopg2
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "opredador_banco-blaze"),
    "database": os.getenv("DB_NAME", "opredador"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASS", "125320"),
    "port": os.getenv("DB_PORT", "5432"),
    "connect_timeout": 10,
}


import time

def get_conn(dict_cursor=True):
    retries = 5
    while retries > 0:
        try:
            conn = psycopg2.connect(**DB_CONFIG)
            if dict_cursor:
                conn.cursor_factory = RealDictCursor
            return conn
        except Exception as e:
            retries -= 1
            if retries == 0:
                print(f"❌ [DB] Falha crítica ao conectar no banco: {e}", flush=True)
                raise e
            print(f"⚠️ [DB] Erro ao conectar (tentando novamente em 5s... {retries} restando): {e}", flush=True)
            time.sleep(5)


def setup_tables():
    conn = get_conn(dict_cursor=False)
    cur = conn.cursor()

    # Tabela de resultados de padrões (40 combos)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pattern_stats (
            id VARCHAR(60) PRIMARY KEY,
            type VARCHAR(10) NOT NULL,
            size INTEGER NOT NULL,
            period_hours INTEGER NOT NULL,
            win_rate DECIMAL(5,2) DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            sm INTEGER DEFAULT 0,
            sa INTEGER DEFAULT 0,
            pnl DECIMAL(15,2) DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Tabela de recordes de confluências
    cur.execute("""
        CREATE TABLE IF NOT EXISTS confluence_records (
            id VARCHAR(80) PRIMARY KEY,
            strategies_count INTEGER NOT NULL,
            max_entries INTEGER NOT NULL,
            period_hours INTEGER NOT NULL,
            max_ativos_recorde INTEGER DEFAULT 0,
            max_ativos_wins INTEGER DEFAULT 0,
            max_ativos_losses INTEGER DEFAULT 0,
            max_ativos_set_at TIMESTAMP,
            last_3_occurrences JSONB DEFAULT '[]',
            sm_recorde INTEGER DEFAULT 0,
            sm_recorde_set_at TIMESTAMP,
            sa_atual INTEGER DEFAULT 0,
            webhook_triggered_at TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Tabela de sinais ao vivo
    cur.execute("""
        CREATE TABLE IF NOT EXISTS live_signals (
            id SERIAL PRIMARY KEY,
            pattern_id VARCHAR(80),
            type VARCHAR(30),
            message TEXT,
            data_json JSONB DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Tabela de robôs configurados pelos usuários
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_patterns (
            id SERIAL PRIMARY KEY,
            user_id VARCHAR(100),
            bot_name VARCHAR(100),
            elements JSONB NOT NULL,
            target VARCHAR(20) NOT NULL,
            max_entries INTEGER DEFAULT 3,
            is_active BOOLEAN DEFAULT TRUE,
            target_telegram_id VARCHAR(50),
            auto_generated BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # Adicionar colunas extras se não existirem
    try:
        cur.execute("ALTER TABLE user_patterns ADD COLUMN IF NOT EXISTS auto_generated BOOLEAN DEFAULT FALSE;")
        cur.execute("ALTER TABLE user_patterns ADD COLUMN IF NOT EXISTS stake_config JSONB DEFAULT '{}';")
        cur.execute("ALTER TABLE user_patterns ADD COLUMN IF NOT EXISTS min_confluence INTEGER DEFAULT 1;")
        cur.execute("ALTER TABLE user_patterns ADD COLUMN IF NOT EXISTS trend_config JSONB DEFAULT '{}';")
    except: pass

    # Tabela de configs de estratégia (para auto-recálculo)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS strategy_configs (
            id SERIAL PRIMARY KEY,
            user_id VARCHAR(100) NOT NULL,
            target_telegram_id VARCHAR(50) NOT NULL,
            name VARCHAR(100) DEFAULT 'Estratégia IA',
            filters JSONB NOT NULL,
            min_confluence INTEGER DEFAULT 1,
            auto_refresh BOOLEAN DEFAULT TRUE,
            last_refresh TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    try:
        cur.execute("ALTER TABLE strategy_configs ADD COLUMN IF NOT EXISTS name VARCHAR(100) DEFAULT 'Estratégia IA';")
        cur.execute("ALTER TABLE strategy_configs ADD COLUMN IF NOT EXISTS min_confluence INTEGER DEFAULT 1;")
    except: pass
    # Tabela de histórico de sinais (Placar Real)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signal_history (
            id SERIAL PRIMARY KEY,
            chat_id VARCHAR(50) NOT NULL,
            target_color VARCHAR(20) NOT NULL,
            result VARCHAR(20) NOT NULL, -- WIN_G0, WIN_G1, WIN_G2, LOSS
            confluences INTEGER DEFAULT 1,
            bot_names TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Tabela de sessões de operação (Tracker de ganhos/perdas por grupo/dia)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS group_sessions (
            id SERIAL PRIMARY KEY,
            target_telegram_id VARCHAR(50) NOT NULL,
            session_start TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            session_end TIMESTAMP,
            is_active BOOLEAN DEFAULT TRUE,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            gales JSONB DEFAULT '{}',
            pnl DECIMAL(15,2) DEFAULT 0,
            total_signals INTEGER DEFAULT 0
        );
    """)

    conn.commit()
    cur.close()
    conn.close()
    print("✅ [DB] Tabelas verificadas/criadas.", flush=True)
