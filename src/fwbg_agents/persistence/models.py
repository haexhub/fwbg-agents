"""ORM models. Kept minimal for M1; expanded with strategy/plugin/transition tables in M2."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from fwbg_agents.persistence.database import Base


class CalibrationRun(Base):
    """Record of one Calibrator pass over fwbg's test_results."""

    __tablename__ = "calibration_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    runs_scanned: Mapped[int] = mapped_column(Integer, nullable=False)
    runs_with_elite: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_classes_processed: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    baseline_path: Mapped[str] = mapped_column(String(512), nullable=False)
