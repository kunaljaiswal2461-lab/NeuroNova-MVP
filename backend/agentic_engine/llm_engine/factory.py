"""Which model client a database-chat turn uses (SPEC-09): the scripted stub when one is configured, else OpenAI."""
from __future__ import annotations

from app.core.config import Settings
from agentic_engine.llm_engine.base_llm import LLMClient
from agentic_engine.llm_engine.openai_client import build_client


def llm_available(settings: Settings) -> bool:
    return bool(settings.nl_sql_stub_llm_path or settings.openai_api_key)


def build_llm(settings: Settings) -> LLMClient:
    if settings.nl_sql_stub_llm_path:
        # Settings refuses this value when APP_ENV=prod; the import stays local so production never loads it.
        from agentic_engine.llm_engine.scripted_client import ScriptedLLM

        return ScriptedLLM(settings.nl_sql_stub_llm_path)       # type: ignore[return-value]
    return build_client(settings.openai_api_key)
