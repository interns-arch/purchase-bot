"""Concrete `DocumentUnderstandingProvider` implementations.

This is the ONLY package allowed to talk to an external model API. Everything
else in the application depends on `backend.app.ai.provider` (the Protocol) and
`backend.app.ai.schemas` (the normalized shapes), so a provider can be swapped
by configuration without touching import or business logic.

Modules here are imported lazily by `backend.app.ai.registry` so an unused
provider never enters the import graph.
"""
