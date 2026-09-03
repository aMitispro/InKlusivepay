from datetime import datetime, timezone
from sqlalchemy import DateTime, ForeignKey, Integer, String, Float
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from enum import Enum

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    wallet : Mapped[int] = mapped_column(Integer, default=100)
    created: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    phone_number: Mapped[str] = mapped_column(String(10), nullable=False)

    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
    )

class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    receiver_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    from_user: Mapped["User"] = relationship(back_populates="transactions")
    to_user: Mapped["User"] = relationship(back_populates="transactions")