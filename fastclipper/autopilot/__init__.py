"""Piloto Automático: watchlist → corte → fila de aprovação humana → Buffer."""

from fastclipper.autopilot.config import AutopilotConfig, load_config
from fastclipper.autopilot.state import AutopilotState

__all__ = ["AutopilotConfig", "AutopilotState", "load_config"]
