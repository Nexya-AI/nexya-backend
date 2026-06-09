"""Fiches de connaissance factuelle injectees conditionnellement dans le
system prompt NEXYA (partenaires, entites connues). Distinct des corpus RAG
(expert_corpus_chunks) : ce sont de petites fiches curatees, declenchees par
detection d'entite dans le message, sans DB ni embedding."""

from app.ai.knowledge.ab_consulting import build_partner_context

__all__ = ["build_partner_context"]
