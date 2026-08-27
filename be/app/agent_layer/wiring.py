from __future__ import annotations

from functools import lru_cache

from app.config import Settings, get_settings
from app.db import get_session_factory

from .gemini import GeminiAgent
from .gmail_gateway import GmailMailGateway
from .ports import MailGateway
from .repository import SqlAlchemyAgentRepository
from .service import AgentOrchestrator


def build_agent_orchestrator(
    *, mail_gateway: MailGateway, settings: Settings | None = None
) -> AgentOrchestrator:
    """Composition hook for the platform-owned FastAPI/Gmail scaffold."""

    resolved = settings or get_settings()
    return AgentOrchestrator(
        repository=SqlAlchemyAgentRepository(session_factory=get_session_factory()),
        mail=mail_gateway,
        agent=GeminiAgent.from_settings(resolved),
    )


def build_default_agent_orchestrator(settings: Settings | None = None) -> AgentOrchestrator:
    """Production composition using the live database, Gemini, and connected Gmail."""

    resolved = settings or get_settings()
    sessions = get_session_factory()
    return AgentOrchestrator(
        repository=SqlAlchemyAgentRepository(session_factory=sessions),
        mail=GmailMailGateway(session_factory=sessions, settings=resolved),
        agent=GeminiAgent.from_settings(resolved),
    )


@lru_cache
def get_default_agent_orchestrator() -> AgentOrchestrator:
    """FastAPI dependency: one immutable composition per backend process."""

    return build_default_agent_orchestrator()
