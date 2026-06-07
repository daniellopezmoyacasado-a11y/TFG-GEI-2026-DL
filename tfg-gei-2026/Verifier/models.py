from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import declarative_base


Base = declarative_base()


class PendingPresentation(Base):
    __tablename__ = "pending_presentations"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, unique=True, index=True, nullable=False)
    dataset_id = Column(Integer, ForeignKey("genomic_datasets.id"), nullable=False)
    request_url = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="pending")
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime(timezone=True), nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)


class GenomicDataset(Base):
    __tablename__ = "genomic_datasets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    file_size = Column(String, nullable=False)
    num_downloads = Column(Integer, nullable=False)
    description = Column(Text, nullable=True)
    required_visa_value = Column(String, nullable=False)
    color = Column(String, nullable=False)
