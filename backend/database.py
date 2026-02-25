"""Database models and setup using SQLAlchemy + SQLite."""
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey, UniqueConstraint
from sqlalchemy.pool import NullPool
try:
    from sqlalchemy.orm import declarative_base, relationship
except ImportError:
    from sqlalchemy.ext.declarative import declarative_base
    from sqlalchemy.orm import relationship
from sqlalchemy.orm import sessionmaker
from datetime import datetime

DATABASE_URL = "sqlite:///./trading_platform.db"

# NullPool: SQLite is a file DB - no connection pooling needed.
# Background tasks call next(get_db()) without triggering finally-close,
# so NullPool prevents "QueuePool limit reached" errors by skipping pooling.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    email = Column(String, unique=True, index=True, nullable=True)
    balance = Column(Float, default=100000.0)
    created_at = Column(DateTime, default=datetime.utcnow)

    settings = relationship("Settings", back_populates="user")
    trades = relationship("Trade", back_populates="user")
    positions = relationship("Position", back_populates="user")
    watched_stocks = relationship("WatchedStock", back_populates="user")


class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
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

    user = relationship("User", back_populates="trades")


class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    symbol = Column(String, index=True)
    quantity = Column(Float, default=0)
    avg_cost = Column(Float, default=0)
    current_price = Column(Float, default=0)
    unrealized_pnl = Column(Float, default=0)
    last_updated = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="positions")
    __table_args__ = (UniqueConstraint('user_id', 'symbol', name='_user_symbol_uc'),)


class AISignal(Base):
    __tablename__ = "ai_signals"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # Can be global or user-specific
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
    user_id = Column(Integer, ForeignKey("users.id"))
    symbol = Column(String, index=True)
    name = Column(String, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="watched_stocks")
    __table_args__ = (UniqueConstraint('user_id', 'symbol', name='_user_watched_symbol_uc'),)


class Settings(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    key = Column(String)
    value = Column(Text)

    user = relationship("User", back_populates="settings")
    __table_args__ = (UniqueConstraint('user_id', 'key', name='_user_setting_key_uc'),)


def create_tables():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_setting(db, key: str, user_id: int, default=None):
    s = db.query(Settings).filter(Settings.user_id == user_id, Settings.key == key).first()
    return s.value if s else default


def set_setting(db, key: str, value: str, user_id: int):
    s = db.query(Settings).filter(Settings.user_id == user_id, Settings.key == key).first()
    if s:
        s.value = value
    else:
        s = Settings(user_id=user_id, key=key, value=value)
        db.add(s)
    db.commit()
