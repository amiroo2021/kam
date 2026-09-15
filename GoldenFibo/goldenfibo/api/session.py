"""Compatibility shim — SessionController is the multi-mode owner."""

from ..session.controller import SessionController, get_session, reset_session_for_tests

LiveSession = SessionController

__all__ = ["LiveSession", "SessionController", "get_session", "reset_session_for_tests"]
