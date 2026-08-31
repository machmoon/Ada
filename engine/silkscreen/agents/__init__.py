"""Model-driven stages: prompt -> circuit -> board -> review.

The engine below this package is deliberately model-free so the parts that must
be *correct* can be tested without a network. This package is the only place a
model call happens, and every stage checks the model rather than trusting it.
"""

from .datasheet import PartFacts, read_datasheet
from .model import Document, GeminiModel, Model, ModelError, ScriptedModel
from .pipeline import PipelineResult, generate_pcb
from .propose import ProposalError, propose_circuit
from .review import Finding, Severity, review_circuit
from .transcribe import transcribe_audio

__all__ = [
    "Model", "GeminiModel", "ScriptedModel", "ModelError", "Document",
    "PartFacts", "read_datasheet",
    "propose_circuit", "ProposalError",
    "Finding", "Severity", "review_circuit",
    "generate_pcb", "PipelineResult",
    "transcribe_audio",
]
