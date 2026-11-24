from datetime import datetime
from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    sites: Mapped[list["Site"]] = relationship("Site", back_populates="owner")
    logs: Mapped[list["OperationLog"]] = relationship("OperationLog", back_populates="user")


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # slug
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    # statuses: pending / creating / running / paused / error / deleting

    wp_port: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    pma_port: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)

    template_id: Mapped[str] = mapped_column(String(128), nullable=False)
    wp_admin_user: Mapped[str] = mapped_column(String(64), nullable=False)
    wp_admin_pass: Mapped[str] = mapped_column(String(128), nullable=False)
    wp_admin_email: Mapped[str] = mapped_column(String(128), nullable=False)
    wp_title: Mapped[str] = mapped_column(String(256), nullable=False)
    db_password: Mapped[str] = mapped_column(String(64), nullable=False)

    current_stage: Mapped[str] = mapped_column(String(16), default="develop")
    # stages: develop / qa / prod
    domain_develop: Mapped[str | None] = mapped_column(String(256), nullable=True)
    domain_qa: Mapped[str | None] = mapped_column(String(256), nullable=True)
    domain_prod: Mapped[str | None] = mapped_column(String(256), nullable=True)

    site_path: Mapped[str] = mapped_column(String(512), nullable=False)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)

    # SHA-256 hex digest of a per-site API key (secrets.token_urlsafe(32) — high
    # entropy random token, not a human password, so no bcrypt needed here).
    # NULL until rotate-key is called; existing sites backfill via that endpoint.
    api_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner: Mapped["User"] = relationship("User", back_populates="sites")
    logs: Mapped[list["OperationLog"]] = relationship(
        "OperationLog", back_populates="site", cascade="all, delete-orphan"
    )


class PortReservation(Base):
    __tablename__ = "port_reservations"

    port: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("sites.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="reserved")
    # statuses: reserved / active / released
    reserved_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OperationLog(Base):
    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[str] = mapped_column(String(64), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(16), default="pending")
    stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    site: Mapped["Site"] = relationship("Site", back_populates="logs")
    user: Mapped["User | None"] = relationship("User", back_populates="logs")
