"""Resolve a selector token (NAME or UUID-prefix) to one or more sessions."""

from __future__ import annotations

from dataclasses import dataclass

from .sweep import SessionInfo


class SelectorError(Exception):
    """Raised on no-match or ambiguous-prefix selector resolution."""

    pass


def resolve_one(sessions: list[SessionInfo], token: str) -> SessionInfo:
    """Newest-match-wins resolution for cat/head/tail.

    Names first; UUID prefix fallback. Returns the most recent match.
    """
    name_matches = [s for s in sessions if s.meta.name == token]
    if name_matches:
        # Already sorted newest-first by caller.
        return name_matches[0]
    uuid_matches = [s for s in sessions if s.id.startswith(token)]
    if not uuid_matches:
        raise SelectorError(f"no such session: {token}")
    if len(uuid_matches) > 1:
        ids = ", ".join(s.id[:8] for s in uuid_matches)
        raise SelectorError(f"ambiguous selector '{token}': matches {ids}")
    return uuid_matches[0]


def resolve_many(sessions: list[SessionInfo], token: str) -> list[SessionInfo]:
    """All-matches resolution for rm.

    Names first (every match); UUID-prefix fallback (unique required).
    """
    name_matches = [s for s in sessions if s.meta.name == token]
    if name_matches:
        return name_matches
    uuid_matches = [s for s in sessions if s.id.startswith(token)]
    if not uuid_matches:
        raise SelectorError(f"no such session: {token}")
    if len(uuid_matches) > 1:
        ids = ", ".join(s.id[:8] for s in uuid_matches)
        raise SelectorError(f"ambiguous selector '{token}': matches {ids}")
    return uuid_matches
