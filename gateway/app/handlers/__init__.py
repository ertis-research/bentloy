"""Request handlers exposed by the gateway service."""

from .info_handler import InfoHandler
from .predict_handler import PredictHandler

__all__ = [
    "InfoHandler",
    "PredictHandler",
]
