"""Provider-independent document-understanding layer (Architecture V2, Phase 1).

Phase 1 is SCAFFOLDING ONLY: schemas, a provider Protocol, a registry that
defaults to `NullProvider`, and a compact-representation builder. Nothing here
is wired into the import pipeline yet, so behaviour is byte-for-byte identical
to before -- see `ARCHITECTURE_V2_PLAN.md` §16 (phases).

Design rules (enforced by review, not just convention):
- No vendor SDK may be imported outside `providers/`.
- The LLM never decides business facts: identity, stock, allocation, FIFO,
  quantities-of-record. It only proposes a normalized reading of a document,
  which `core.services.normalized_validation` then re-checks server-side.
- Everything degrades to today's deterministic behaviour when disabled.
"""
