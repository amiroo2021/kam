"""Namespace package marker for the KAM add-on payload.

Hermes ships its own ``plugins/__init__.py``; this repository mirrors it so
the payload is importable directly from the checkout. That is what lets
``verify.sh --dry-run-source`` and the offline test suite import
``plugins.trade`` before anything has been installed.

This file is **not** part of the install payload: the installer copies only
``plugins/trade/**``, so Hermes' own ``plugins/__init__.py`` is never
touched or overwritten.
"""
