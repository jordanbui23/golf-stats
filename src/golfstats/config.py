from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


@dataclass
class Config:
    data_dir: Path
    player: str = ""
    handedness: str = "R"
    preferred_shape: str = "straight"
    gap_minutes: float = 90
    min_shots: int = 8
    smash_index_min: float = 95.0
    impact_offset_spread_mm: float = 8.0
    low_point_behind_share: float = 0.25
    curve_bias_pct: float = 4.0
    face_window: tuple[float, float] = (-2.0, 2.0)
    plan_shots: int = 20
    face_to_path_windows: dict[str, tuple[float, float]] = field(default_factory=lambda: {
        "straight": (-2.0, 2.0), "draw": (-4.0, 0.0), "fade": (0.0, 4.0)})

    @property
    def db_path(self) -> Path:
        return self.data_dir / "golf.db"

    @property
    def inbox(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def archive(self) -> Path:
        return self.data_dir / "raw"

    @property
    def reports(self) -> Path:
        return self.data_dir / "reports"

    @property
    def plans(self) -> Path:
        return self.data_dir / "plans"

    @property
    def dashboard(self) -> Path:
        return self.data_dir / "dashboard.html"

    @property
    def face_to_path_window(self) -> tuple[float, float]:
        return self.face_to_path_windows[self.preferred_shape]


def load_config(path: Path | None = None, data_dir: Path | None = None) -> Config:
    path = path or REPO / "config.toml"
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    player, sessions, focus = raw.get("player", {}), raw.get("sessions", {}), raw.get("focus", {})
    base = Path(raw.get("data_dir", "data"))
    cfg = Config(data_dir=data_dir or (base if base.is_absolute() else REPO / base))
    cfg.player = player.get("name", cfg.player)
    cfg.handedness = player.get("handedness", cfg.handedness).upper()
    cfg.preferred_shape = player.get("preferred_shape", cfg.preferred_shape).lower()
    cfg.gap_minutes = sessions.get("gap_minutes", cfg.gap_minutes)
    for key in ("min_shots", "smash_index_min", "impact_offset_spread_mm", "low_point_behind_share",
                "curve_bias_pct", "plan_shots"):
        setattr(cfg, key, focus.get(key, getattr(cfg, key)))
    cfg.face_window = tuple(focus.get("face_window", cfg.face_window))
    for shape, window in focus.get("face_to_path_window", {}).items():
        cfg.face_to_path_windows[shape] = tuple(window)
    if cfg.handedness != "R":
        raise ValueError("only right-handed analysis is supported (player.handedness = \"R\")")
    if cfg.preferred_shape not in cfg.face_to_path_windows:
        raise ValueError(f"preferred_shape must be one of {sorted(cfg.face_to_path_windows)}")
    return cfg
