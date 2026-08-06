"""Pydantic request and response models — the wire contract.

Normalisation happens here, on the way in, so a handler never receives a value
it has to remember to clean. The database CHECK is the backstop, not the rule.
"""
