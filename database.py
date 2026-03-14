# database.py
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, Float
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from datetime import datetime
import asyncio
from logging_config import get_logger

logger = get_logger('database')

DATABASE_URL = "sqlite:///avito_smart.db"

engine = create_engine(DATABASE_URL, echo=False, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Источники объявлений
SOURCES = ("avito", "cian")

# Воронка статусов для CRM
STATUS_NEW = "new"
STATUS_IN_WORK = "in_work"
STATUS_CALLED = "called"
STATUS_NO_ANSWER = "no_answer"
STATUS_MEETING_SET = "meeting_set"
STATUS_DEAL = "deal"
STATUS_LOST = "lost"
STATUS_CLOSED = "closed"
PIPELINE_STATUSES = (
    STATUS_NEW,
    STATUS_IN_WORK,
    STATUS_CALLED,
    STATUS_NO_ANSWER,
    STATUS_MEETING_SET,
    STATUS_DEAL,
    STATUS_LOST,
    STATUS_CLOSED,
)


class User(Base):
    __tablename__ = "users"
    user_id = Column(Integer, primary_key=True)  # Telegram user_id
    subscription_end = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserFilter(Base):
    __tablename__ = "user_filters"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)  # один пользователь — несколько фильтров (в т.ч. Авито + ЦИАН)
    source = Column(String(20), default="avito", index=True)  # avito | cian
    search_url = Column(String, nullable=True)
    min_price = Column(Integer, nullable=True)
    max_price = Column(Integer, nullable=True)
    city = Column(String, default="Москва")
    district = Column(String, nullable=True)  # район для дополнительного фильтра по адресу
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Ad(Base):
    __tablename__ = "ads"
    id = Column(Integer, primary_key=True)
    avito_id = Column(String, index=True)  # внешний ID объявления (avito или cian)
    user_id = Column(Integer, ForeignKey("user_filters.user_id"), index=True)
    source = Column(String(20), default="avito", index=True)  # avito | cian
    title = Column(String)
    price = Column(String)
    price_value = Column(Float, nullable=True)
    address = Column(String)
    description = Column(Text, nullable=True)
    url = Column(String)
    status = Column(String, default="active")  # active | removed
    removed_at = Column(DateTime, nullable=True)
    custom_phone = Column(String, nullable=True)
    is_favorite = Column(Boolean, default=False)
    status_pipeline = Column(String(30), default=STATUS_NEW, index=True)  # воронка CRM
    notes = Column(Text, nullable=True)
    last_contact_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    photos = relationship("Photo", back_populates="ad", cascade="all, delete-orphan", lazy="selectin")

class Photo(Base):
    __tablename__ = "photos"
    id = Column(Integer, primary_key=True)
    ad_id = Column(Integer, ForeignKey("ads.id"), index=True)
    file_path = Column(String)
    is_main = Column(Boolean, default=False)
    ad = relationship("Ad", back_populates="photos")

def _ensure_column(conn, table, column_name, sql_type_default):
    result = conn.exec_driver_sql(f"PRAGMA table_info({table})")
    columns = [row[1] for row in result]
    if column_name not in columns:
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column_name} {sql_type_default}")
        logger.info(f"🛠 Добавлен столбец {table}.{column_name}")

def check_subscription(user_id: int) -> bool:
    """Возвращает True, если у пользователя активная подписка."""
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.user_id == user_id).first()
        if not u or u.subscription_end is None:
            return False
        from datetime import datetime
        return u.subscription_end >= datetime.utcnow()
    finally:
        db.close()


def get_or_create_user(user_id: int) -> User:
    """Создаёт или возвращает пользователя."""
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.user_id == user_id).first()
        if not u:
            u = User(user_id=user_id)
            db.add(u)
            db.commit()
            db.refresh(u)
        return u
    finally:
        db.close()


def grant_subscription(user_id: int, days: int) -> bool:
    """Выдаёт подписку на N дней. Продлевает от subscription_end или от now."""
    from datetime import datetime, timedelta
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.user_id == user_id).first()
        if not u:
            u = User(user_id=user_id)
            db.add(u)
            db.commit()
            db.refresh(u)
        base = u.subscription_end if u.subscription_end and u.subscription_end >= datetime.utcnow() else datetime.utcnow()
        u.subscription_end = base + timedelta(days=days)
        db.commit()
        return True
    except Exception as e:
        logger.error(f"grant_subscription: {e}")
        return False
    finally:
        db.close()


def _run_migrations():
    try:
        with engine.connect() as conn:
            result = conn.exec_driver_sql("PRAGMA table_info(ads)")
            columns_ads = [row[1] for row in result]
            if "is_favorite" not in columns_ads:
                _ensure_column(conn, "ads", "is_favorite", "BOOLEAN DEFAULT 0")
            if "source" not in columns_ads:
                _ensure_column(conn, "ads", "source", "VARCHAR(20) DEFAULT 'avito'")
            if "status_pipeline" not in columns_ads:
                _ensure_column(conn, "ads", "status_pipeline", "VARCHAR(30) DEFAULT 'new'")
            if "notes" not in columns_ads:
                _ensure_column(conn, "ads", "notes", "TEXT")
            if "last_contact_at" not in columns_ads:
                _ensure_column(conn, "ads", "last_contact_at", "DATETIME")

            result_uf = conn.exec_driver_sql("PRAGMA table_info(user_filters)")
            columns_uf = [row[1] for row in result_uf]
            if "source" not in columns_uf:
                _ensure_column(conn, "user_filters", "source", "VARCHAR(20) DEFAULT 'avito'")
            if "district" not in columns_uf:
                _ensure_column(conn, "user_filters", "district", "VARCHAR(255)")

            conn.commit()
    except Exception as e:
        logger.error(f"❌ Ошибка миграции БД: {e}")

def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        _run_migrations()
        logger.info("✅ База данных готова")
    except Exception as e:
        logger.error(f"❌ Ошибка БД: {e}")
        raise

async def run_in_thread(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)