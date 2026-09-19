"""perception/ - the ONLY package that calls an LLM.

The model produces structured facts (ImageExtraction, Amendment); every number
that reaches the output is computed by code elsewhere.
"""
from buyorwait.perception.cache import ResponseCache, cache_key
from buyorwait.perception.client import CallStats, ModelClient, validate_against_schema
from buyorwait.perception.extract import extract_image, interpret_messages
from buyorwait.perception.fallback import Fallback, give_up
from buyorwait.perception.run import PerceptionContext, run_perception
from buyorwait.perception.tools import ToolBox

__all__ = [
    "CallStats", "Fallback", "ModelClient", "PerceptionContext", "ResponseCache",
    "ToolBox", "cache_key", "extract_image", "give_up", "interpret_messages",
    "run_perception", "validate_against_schema",
]
