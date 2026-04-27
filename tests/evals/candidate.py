"""
Évals IA — Génération de la réponse candidate.

Pour chaque question du corpus, on doit produire une réponse NEXYA à
juger. Deux stratégies selon la catégorie :

1. **`routing`** : pure introspection de `EXPERT_REGISTRY` —
   `expected_provider`/`expected_model` → on lit le registre. **Aucun
   appel LLM**, juste une assertion de contrat. Le « score » devient
   binaire (10.0 si match, 0.0 sinon) et on saute le juge sémantique.

2. **`safety` / `format` / `accuracy` / `identity`** : appel direct
   Gemini via `google-genai` avec le `system_prompt` de l'expert tel
   que défini dans `EXPERT_REGISTRY`, et le `primary_model` de l'expert.
   On ne passe PAS par `StreamHandler.stream()` (trop d'infra : DB,
   Redis, FastAPI Request, lifespan) — on isole le LLM call pur, ce
   qui suffit pour évaluer la qualité de réponse de la pile NEXYA.

Discipline déterministe :
- `temperature=0.0` forcé (pour réduire la variance run-à-run).
- Pas de tools, pas de cache, pas de RAG corpus G1 (l'eval teste le
  LLM brut + system_prompt expert, pas la couche d'enrichissement).

Skip gracieux : si `GEMINI_API_KEY` vide ET tier non-mock, retourne
un placeholder reasoning='no_api_key' pour que le harness continue.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from app.ai.experts import EXPERT_REGISTRY, get_expert_config

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class CandidateAnswer:
    """Réponse générée à juger.

    `is_synthetic_routing` : True pour la catégorie `routing` où on a
    fait un check d'intégrité du registre, pas un vrai appel LLM. Le
    runner skip le juge sémantique dans ce cas et utilise un Verdict
    direct basé sur le match.
    """

    text: str
    is_synthetic_routing: bool = False
    routing_match: bool = False  # uniquement pertinent si is_synthetic_routing


# ═══════════════════════════════════════════════════════════════════
# DISPATCH PAR CATÉGORIE
# ═══════════════════════════════════════════════════════════════════


async def generate_candidate_answer(
    *,
    category: str,
    question_text: str,
    expert_id: str | None,
    expected_provider: str | None = None,
    expected_model: str | None = None,
    mock_candidate: bool = False,
) -> CandidateAnswer:
    """Génère la réponse candidate pour une question.

    Dispatch :
    - `category == "routing"` → introspection registre (pas de LLM)
    - autres → appel LLM via Gemini SDK avec system_prompt expert
    - `mock_candidate=True` → réponse synthétique sans appel LLM
      (utilisé en tandem avec MockJudge pour test du harness CI gratuit).
    """
    if category == "routing":
        return _check_routing(
            expert_id=expert_id,
            expected_provider=expected_provider,
            expected_model=expected_model,
        )
    if mock_candidate:
        return _mock_answer(question_text=question_text, expert_id=expert_id)
    return await _llm_answer(question_text=question_text, expert_id=expert_id)


def _mock_answer(*, question_text: str, expert_id: str | None) -> CandidateAnswer:
    """Réponse synthétique déterministe (pour test pipeline sans LLM)."""
    config = get_expert_config(expert_id)
    return CandidateAnswer(
        text=(
            f"[NEXYA mock-answer expert={config.expert_id}] "
            f"Réponse synthétique pour : {question_text[:120]}"
        )
    )


# ═══════════════════════════════════════════════════════════════════
# ROUTING — pure introspection
# ═══════════════════════════════════════════════════════════════════


def _check_routing(
    *,
    expert_id: str | None,
    expected_provider: str | None,
    expected_model: str | None,
) -> CandidateAnswer:
    """Vérifie que `EXPERT_REGISTRY[expert_id]` route vers le bon
    `(provider, model)`. Pas d'appel LLM."""
    config = get_expert_config(expert_id)
    actual_provider = config.primary_provider
    actual_model = config.primary_model

    expected_p = expected_provider or actual_provider
    expected_m = expected_model or actual_model
    match = (actual_provider == expected_p) and (actual_model == expected_m)

    text = (
        f"expert_id={config.expert_id} → "
        f"provider={actual_provider}, model={actual_model} "
        f"(attendu : provider={expected_p}, model={expected_m}) "
        f"→ {'MATCH' if match else 'MISMATCH'}"
    )
    return CandidateAnswer(
        text=text,
        is_synthetic_routing=True,
        routing_match=match,
    )


# ═══════════════════════════════════════════════════════════════════
# LLM ANSWER — appel direct Gemini SDK
# ═══════════════════════════════════════════════════════════════════


async def _llm_answer(*, question_text: str, expert_id: str | None) -> CandidateAnswer:
    """Appel Gemini direct avec system_prompt expert + primary_model.

    Fail-safe : si la clé manque ou que le SDK lève, retourne un
    placeholder pour que le run continue (le juge donnera 0).
    """
    from app.config import settings

    config = get_expert_config(expert_id)

    # Mock-first : si pas de clé API, on retourne un placeholder
    # déterministe basé sur le couple (expert_id, question). Ça permet
    # au harness de tourner en CI sans clé (avec MockJudge en bout) sans
    # que le runner crash.
    if not settings.gemini_api_key and not settings.gemini_use_vertex:
        return CandidateAnswer(
            text=(
                f"[NEXYA mock-answer expert={config.expert_id}] "
                f"Réponse synthétique pour : {question_text[:120]}"
            )
        )

    try:
        text = await _call_gemini(
            model=config.primary_model,
            system_prompt=config.system_prompt,
            user_message=question_text,
        )
    except Exception as exc:  # noqa: BLE001 — fail-safe absolu
        log.warning(
            "evals.candidate.llm_error",
            error=str(exc),
            expert_id=config.expert_id,
        )
        return CandidateAnswer(
            text=f"[NEXYA error: {type(exc).__name__}]",
        )

    return CandidateAnswer(text=text)


async def _call_gemini(*, model: str, system_prompt: str, user_message: str) -> str:
    """Appel Gemini direct (Vertex AI ou AI Studio). Pattern aligné G1."""
    from google import genai  # noqa: PLC0415
    from google.genai import types  # noqa: PLC0415

    from app.config import settings

    if settings.gemini_use_vertex:
        client = genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.gcp_region,
        )
    else:
        client = genai.Client(api_key=settings.gemini_api_key)

    config_kwargs: dict = {"temperature": 0.0}
    if system_prompt:
        config_kwargs["system_instruction"] = system_prompt

    response = await client.aio.models.generate_content(
        model=model,
        contents=user_message,
        config=types.GenerateContentConfig(**config_kwargs),
    )
    return getattr(response, "text", None) or ""


# ═══════════════════════════════════════════════════════════════════
# UTIL — registry inspection
# ═══════════════════════════════════════════════════════════════════


def routing_expected_for(expert_id: str) -> tuple[str, str]:
    """Retourne `(primary_provider, primary_model)` pour un expert_id.

    Utilisé pour générer dynamiquement les `expected_*` des tests
    routing si le YAML ne les fournit pas (snapshot vivant du registre).
    """
    config = EXPERT_REGISTRY.get(expert_id) or EXPERT_REGISTRY["general"]
    return config.primary_provider, config.primary_model
