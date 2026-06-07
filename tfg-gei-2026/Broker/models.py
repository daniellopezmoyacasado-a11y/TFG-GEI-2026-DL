from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship


Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    user_wallet_id = Column(String, unique=True, nullable=False, index=True)

    assignments = relationship("UserVisa", back_populates="user")


class Visa(Base):
    __tablename__ = "visas"

    id = Column(Integer, primary_key=True, index=True)
    jti = Column(String, unique=True, nullable=False, index=True)
    visa_jwt = Column(Text, unique=True, nullable=False)
    sub = Column(String, nullable=False, index=True)
    visa_type = Column(String, nullable=True)
    visa_value = Column(String, nullable=True)

    assignments = relationship("UserVisa", back_populates="visa")


class UserVisa(Base):
    __tablename__ = "user_visas"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    visa_id = Column(Integer, ForeignKey("visas.id"), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)

    user = relationship("User", back_populates="assignments")
    visa = relationship("Visa", back_populates="assignments")

    __table_args__ = (
        UniqueConstraint("user_id", "visa_id", name="uq_user_visa"),
    )


class PendingPresentation(Base):
    __tablename__ = "pending_presentations"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, unique=True, index=True, nullable=False)
    user_wallet_id = Column(String, index=True, nullable=False)
    email = Column(String, nullable=True)
    request_url = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="pending")
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime(timezone=True), nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)
