"""Database adapters.

Everything that speaks SQLAlchemy lives under here. ``domain/`` never imports
it; ``api/`` reaches it only through the wiring in ``api/deps.py``.
"""
