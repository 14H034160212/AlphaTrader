"""Database models and setup using SQLAlchemy + SQLite."""
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text
try:
    from sqlalchemy.orm import declarative_base
except ImportError:
    from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

DATABASE_URL = "sqlite:///./trading_platform.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)
    side = Column(String)  # BUY or SELL
    quantity = Column(Float)
    price = Column(Float)
    total_value = Column(Float)
    order_type = Column(String, default="MARKET")
    status = Column(String, default="FILLED")
    ai_triggered = Column(Boolean, default=False)
    ai_confidence = Column(Float, nullable=True)
    reasoning = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, unique=True, index=True)
    quantity = Column(Float, default=0)
    avg_cost = Column(Float, default=0)
    current_price = Column(Float, default=0)
    unrealized_pnl = Column(Float, default=0)
    last_updated = Column(DateTime, default=datetime.utcnow)


class AISignal(Base):
    __tablename__ = "ai_signals"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)
    signal = Column(String)  # BUY, SELL, HOLD
    confidence = Column(Float)
    target_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    reasoning = Column(Text)
    model_used = Column(String, default="deepseek-reasoner")
    timestamp = Column(DateTime, default=datetime.utcnow)


class WatchedStock(Base):
    __tablename__ = "watched_stocks"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, unique=True, index=True)
    name = Column(String, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)


class Settings(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True)
    value = Column(Text)


def create_tables():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_setting(db, key: str, default=None):
    s = db.query(Settings).filter(Settings.key == key).first()
    return s.value if s else default


def set_setting(db, key: str, value: str):
    s = db.query(Settings).filter(Settings.key == key).first()
    if s:
        s.value = value
    else:
        s = Settings(key=key, value=value)
        db.add(s)
    db.commit()
