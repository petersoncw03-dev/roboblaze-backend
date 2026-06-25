import os
import logging
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import Column, Integer, String, DateTime, Float
from urllib.parse import urlparse

# Configura log local para o banco
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
db_logger = logging.getLogger("DB_INIT")

# Puxa a URL do banco do arquivo .env ou do painel Easypanel
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/roboblaze")

engine = create_async_engine(
    DATABASE_URL, 
    echo=False,
    pool_pre_ping=True,
    pool_recycle=3600
)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

class Result(Base):
    __tablename__ = "results"
    id = Column(String, primary_key=True, index=True)
    color = Column(String, index=True)
    roll = Column(Integer)
    timestamp = Column(DateTime(timezone=True), index=True)
    total_bets = Column(Float, nullable=True)
    total_payout = Column(Float, nullable=True)
    house_profit = Column(Float, nullable=True)

async def init_db():
    # Mascarar a senha para o log
    url = urlparse(DATABASE_URL.replace("postgresql+asyncpg://", "http://"))
    db_info = f"Host: {url.hostname} | Banco: {url.path[1:]}"
    
    db_logger.info(f"🔗 Tentando conectar no Banco de Dados... ({db_info})")
    
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        db_logger.info(f"✅ Conexão estabelecida com sucesso! ({db_info})")
    except Exception as e:
        db_logger.error(f"❌ ERRO NA CONEXÃO ({db_info}): {e}")
        raise e
