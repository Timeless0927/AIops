"""K8s execution contracts shared by Gateway and Connector."""

from .command_envelope import CommandEnvelope
from .result_envelope import ResultEnvelope

__all__ = [
    "CommandEnvelope",
    "ResultEnvelope",
]
