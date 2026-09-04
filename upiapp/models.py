from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base, UserMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    wallet: Mapped[float] = mapped_column(Float, default=100.0, nullable=False)
    qr_image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    profile_image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    transaction_pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    sent_transactions: Mapped[list["Transaction"]] = relationship(
        foreign_keys="[Transaction.sender_id]",
        back_populates="sender",
        cascade="all, delete-orphan",
    )
    received_transactions: Mapped[list["Transaction"]] = relationship(
        foreign_keys="[Transaction.receiver_id]",
        back_populates="receiver",
        cascade="all, delete-orphan",
    )
    family_memberships: Mapped[list["FamilyMember"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    favorite_contacts: Mapped[list["FavoriteContact"]] = relationship(
        "FavoriteContact",
        foreign_keys="FavoriteContact.user_id",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<User (id={self.id}, username='{self.username}', email='{self.email}')>"


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    sender_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    receiver_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="SUCCESS", nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(20), default="TRANSFER", nullable=False)
    created_on: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    sender: Mapped["User | None"] = relationship(foreign_keys=[sender_id], back_populates="sent_transactions")
    receiver: Mapped["User"] = relationship(foreign_keys=[receiver_id], back_populates="received_transactions")

    def __repr__(self):
        return f"<Transaction (id={self.id}, amount={self.amount}, status={self.status})>"


class FamilyCircle(Base):
    __tablename__ = "family_circles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    admin_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    daily_limit: Mapped[float] = mapped_column(Float, default=5000.0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    invite_code: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    admin: Mapped["User"] = relationship(foreign_keys=[admin_id])
    members: Mapped[list["FamilyMember"]] = relationship(
        back_populates="family",
        cascade="all, delete-orphan",
    )
    requests: Mapped[list["FamilyTransferRequest"]] = relationship(
        back_populates="family",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<FamilyCircle (id={self.id}, name={self.name}, limit={self.daily_limit})>"


class FamilyMember(Base):
    __tablename__ = "family_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("family_circles.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(30), default="MEMBER", nullable=False)  # ADMIN, PRIMARY_GUARDIAN, BACKUP_GUARDIAN, MEMBER
    relation: Mapped[str] = mapped_column(String(50), default="Family Member", nullable=False)  # Parent, Daughter, Son, Spouse, Elder, etc.
    custom_daily_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    family: Mapped["FamilyCircle"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="family_memberships")

    def __repr__(self):
        return f"<FamilyMember (id={self.id}, user_id={self.user_id}, role={self.role})>"


class FamilyTransferRequest(Base):
    __tablename__ = "family_transfer_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("family_circles.id"), nullable=False)
    requester_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    recipient: Mapped[str] = mapped_column(String(80), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)  # PENDING, APPROVED, REJECTED
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    family: Mapped["FamilyCircle"] = relationship(back_populates="requests")
    requester: Mapped["User"] = relationship(foreign_keys=[requester_id])

    def __repr__(self):
        return f"<FamilyTransferRequest (id={self.id}, amount={self.amount}, status={self.status})>"


class FavoriteContact(Base):
    __tablename__ = "favorite_contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    contact_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    user: Mapped["User"] = relationship(foreign_keys=[user_id], back_populates="favorite_contacts")
    contact_user: Mapped["User"] = relationship(foreign_keys=[contact_user_id])

    def __repr__(self):
        return f"<FavoriteContact (id={self.id}, user_id={self.user_id}, contact_id={self.contact_user_id}, nickname='{self.nickname}')>"
