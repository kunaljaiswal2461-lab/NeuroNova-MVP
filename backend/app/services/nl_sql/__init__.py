"""NL->SQL pass 1 (SPEC-08): turn a question plus the schema block into one strict envelope. Executes nothing."""
from .clock import rewrite_clock
from .envelope import ENVELOPE_JSON_SCHEMA, EnvelopeError, parse_envelope
from .generate import generate_sql
from .intent import intent_warnings
from .pipeline import AttemptRecord, PipelineEvent, PipelineOutcome, RepairRecord, answer_question
from .prompt import CANARY, PROMPT_VERSION, build_system, build_user
from .types import (
    FAILURE_CODES,
    OUTPUT_TYPES,
    REASON_CODES,
    Envelope,
    LLMCall,
    OutputIntent,
    Pass1Result,
    Turn,
)

__all__ = [
    "AttemptRecord", "PipelineEvent", "PipelineOutcome", "RepairRecord", "answer_question",
    "CANARY", "ENVELOPE_JSON_SCHEMA", "Envelope", "EnvelopeError", "FAILURE_CODES", "LLMCall", "OUTPUT_TYPES",
    "OutputIntent", "PROMPT_VERSION", "Pass1Result", "REASON_CODES", "Turn", "build_system", "build_user",
    "generate_sql", "intent_warnings", "parse_envelope", "rewrite_clock",
]
