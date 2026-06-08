"""Profiling engine — Layer 2.

Takes a raw dataset file path + FileType, produces a ProfileReport covering
schema, statistics, quality, relationships, semantic column types, and an
overall health score.
"""
from agentic_engine.profiler.engine import profile_dataset
from agentic_engine.profiler.report import ProfileReport

__all__ = ["profile_dataset", "ProfileReport"]
