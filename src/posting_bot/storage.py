from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Editor(Base):
    __tablename__ = "editors"
    user_id: Mapped[int] = mapped_column(primary_key=True)
    active_draft_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Draft(Base):
    __tablename__ = "drafts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[int] = mapped_column(index=True)
    site_id: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="site")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(300), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")
    photo_file_id: Mapped[str | None] = mapped_column(Text)
    photo_suffix: Mapped[str | None] = mapped_column(String(8))
    category_slug: Mapped[str | None] = mapped_column(String(100))
    author_slug: Mapped[str | None] = mapped_column(String(100))
    tag_names: Mapped[list] = mapped_column(JSON, default=list)
    option_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    edit_field: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    deliveries: Mapped[list[Delivery]] = relationship(back_populates="draft", cascade="all, delete-orphan")


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("draft_id", "site_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey("drafts.id"), index=True)
    site_id: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(36), unique=True, default=lambda: str(uuid4()))
    stage: Mapped[str] = mapped_column(String(20), default="new")
    article_id: Mapped[str | None] = mapped_column(String(100))
    url: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[int] = mapped_column(Integer, default=0)
    draft: Mapped[Draft] = relationship(back_populates="deliveries")


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"
    update_id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Store:
    def __init__(self, url: str):
        self.engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite:") else {}, pool_pre_ping=True)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> Session:
        return self.sessions()

    @staticmethod
    def editor(session: Session, user_id: int) -> Editor:
        editor = session.get(Editor, user_id, with_for_update=True)
        if editor is None:
            editor = Editor(user_id=user_id)
            session.add(editor)
            session.flush()
        return editor

    @staticmethod
    def active(session: Session, user_id: int) -> Draft | None:
        editor = Store.editor(session, user_id)
        return session.get(Draft, editor.active_draft_id, with_for_update=True) if editor.active_draft_id else None

    @staticmethod
    def new(session: Session, user_id: int) -> Draft:
        editor = Store.editor(session, user_id)
        draft = Draft(user_id=user_id)
        session.add(draft)
        session.flush()
        editor.active_draft_id = draft.id
        return draft

    @staticmethod
    def mark_update(session: Session, update_id: int) -> bool:
        if session.get(ProcessedUpdate, update_id):
            return False
        session.add(ProcessedUpdate(update_id=update_id))
        return True

    def list_drafts(self, user_id: int) -> list[Draft]:
        with self.session() as session:
            return list(session.scalars(select(Draft).where(Draft.user_id == user_id).order_by(Draft.updated_at.desc()).limit(15)))
