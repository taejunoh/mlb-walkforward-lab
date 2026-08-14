from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True)
class RawArtifact:
    source: str
    sha256: str
    data_path: Path
    manifest_path: Path
    retrieved_at_utc: str
    row_count: int


@dataclass(frozen=True)
class GameClock:
    game_pk: int
    official_date: date
    scheduled_start_utc: datetime
    lock_at_utc: datetime
    feature_as_of: date
