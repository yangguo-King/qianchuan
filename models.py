"""数据库模型 - 直播场次/事件/转写/报告/账号"""

import datetime
from pathlib import Path

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, BigInteger, ForeignKey, Index
from sqlalchemy.orm import DeclarativeBase, relationship

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = str(DATA_DIR / "live.db")
ENGINE = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False}, echo=False)

WAL_PRAGMAS = ["PRAGMA journal_mode=WAL", "PRAGMA foreign_keys=ON", "PRAGMA synchronous=NORMAL"]


class Base(DeclarativeBase):
    pass


# ---------- 主表 ----------

class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    live_id = Column(String(128), nullable=False, index=True)
    room_id = Column(String(64))
    anchor_name = Column(String(255))
    room_title = Column(String(255))
    started_at = Column(DateTime, default=datetime.datetime.now)
    ended_at = Column(DateTime)
    video_dir = Column(String(500))
    cookie_file = Column(String(500))
    is_active = Column(Integer, default=1)

    events = relationship("LiveEvent", back_populates="session", cascade="all, delete-orphan")
    transcripts = relationship("Transcript", back_populates="session", cascade="all, delete-orphan")
    reviews = relationship("Review", back_populates="session", cascade="all, delete-orphan")

    def ended(self) -> bool:
        return self.ended_at is not None


# ---------- 事件表 ----------

class LiveEvent(Base):
    __tablename__ = "live_events"
    __table_args__ = (
        Index("idx_events_session_type", "session_id", "event_type"),
        Index("idx_events_session_sec", "session_id", "abs_sec"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(32), nullable=False, index=True)
    abs_sec = Column(Integer)
    user_id = Column(BigInteger)
    user_name = Column(String(255))
    content = Column(Text)
    extra_json = Column(Text)
    received_at = Column(DateTime, default=datetime.datetime.now)

    session = relationship("Session", back_populates="events")


# ---------- 转写表 ----------

class Transcript(Base):
    __tablename__ = "transcripts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    seg_index = Column(Integer, nullable=False)
    start_sec = Column(Integer)
    end_sec = Column(Integer)
    text = Column(Text)
    vision_text = Column(Text)
    words_json = Column(Text)
    mp4_path = Column(String(500))
    created_at = Column(DateTime, default=datetime.datetime.now)

    session = relationship("Session", back_populates="transcripts")


# ---------- 复盘报告表 ----------

class Review(Base):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    report = Column(Text)
    qc_summary = Column(Text)
    dm_summary = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.now)

    session = relationship("Session", back_populates="reviews")


# ---------- 账号表 ----------

class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    live_id = Column(String(128), nullable=False, unique=True)
    anchor_name = Column(String(255))
    cookie_file = Column(String(500))
    last_used = Column(DateTime)
    notes = Column(Text)

class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(128), primary_key=True)
    value = Column(Text, default="")


# ---------- 初始化 ----------

def init_db():
    for pragma in WAL_PRAGMAS:
        with ENGINE.connect() as conn:
            conn.exec_driver_sql(pragma)
    Base.metadata.create_all(ENGINE)
    _auto_migrate()

def _auto_migrate():
    """自动检测并 ALTER TABLE 新增列, 避免 SQLite 不自动迁移的坑"""
    from sqlalchemy import text, inspect
    inspector = inspect(ENGINE)
    expected = {
        "transcripts": {"vision_text": "TEXT"},
        "sessions": {"video_dir": "VARCHAR(500)", "cookie_file": "VARCHAR(500)"},
    }
    for table, cols in expected.items():
        if table not in inspector.get_table_names():
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for col, coltype in cols.items():
            if col not in existing:
                try:
                    with ENGINE.connect() as conn:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
                        conn.commit()
                except Exception:
                    pass  # 已存在则忽略


def get_session():
    from sqlalchemy.orm import Session as DBSession
    return DBSession(ENGINE)
