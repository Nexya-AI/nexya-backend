"""
Mock embeddings provider — déterministe, L2-normalisé, SHA-based, dim configurable.

Activé automatiquement par la factory quand aucune clé embeddings réelle
n'est dispo (OpenAI vide ET Gemini vide), ou via kill-switch
`settings.embeddings_mock_enabled=True`. Pattern mock-first aligné Brevo /
hCaptcha / FCM / ObjectStore / VirusScanner. Permet de développer et
tester toute la Couche Mémoire + le corpus Experts (G1) sans clé réelle.

Discipline :
- **Déterministe** : même texte → même vecteur (reproductible en test).
- **L2-normalisé** : `||vector|| = 1.0` strict (aligné OpenAI/Gemini qui
  retournent des vecteurs déjà normés, utile pour `<=>` pgvector qui lit
  `1 - dot(a,b)` quand les deux opérandes ont norm=1).
- **`dim` configurable** (G1) : 1536 par défaut pour compat D1 (memories,
  document_chunks `vector(1536)`). Le script d'ingestion G1 + les tests
  `expert_corpus_chunks` (dim 768 alignée Gemini `text-embedding-004`)
  instancient `MockEmbeddingsProvider(dim=768)` pour respecter la
  contrainte colonne `vector(768)`.
- **Décorrélation suffisante** pour les tests : deux textes distincts
  produisent des vecteurs différents, pas orthogonaux mais distinguables.

Attention : **sémantique zéro**. Le Mock ne comprend pas le texte.
Deux textes proches en sens (« Je suis dev Flutter », « Je code en
Flutter ») produisent des vecteurs aussi éloignés que deux textes sans
lien. Les tests sémantiques réels doivent `@pytest.mark.skipif(not
GEMINI_API_KEY)`.

Algorithme (simple, sûr, rapide) :
    1. SHA-256 du texte UTF-8 → 32 bytes (256 bits).
    2. Étirer par répétition jusqu'à >= `dim` bytes.
    3. Re-centrer : `(byte - 127.5) / 127.5` → valeurs dans [-1, 1].
    4. Tronquer à `dim` floats, L2-normaliser → ||v|| = 1.
"""

from __future__ import annotations

import hashlib
import math

import structlog

from app.ai.embeddings.base import (
    EmbeddingsInvalidRequestError,
    EmbeddingsProvider,
    EmbeddingsResponse,
    EmbeddingsUsage,
    EmbeddingVector,
)

log = structlog.get_logger()


# Dim par défaut alignée D1 (memories + document_chunks vector(1536)).
_DEFAULT_DIM: int = 1536


class MockEmbeddingsProvider(EmbeddingsProvider):
    """Embeddings synthétiques SHA-based, L2-normalisés, dim configurable.

    Instance partagée via la factory (un singleton par `dim`). Pas d'état
    mutable → thread/async-safe.
    """

    name: str = "mock"

    def __init__(self, *, dim: int = _DEFAULT_DIM) -> None:
        if dim <= 0:
            raise ValueError(f"MockEmbeddingsProvider: dim doit être > 0, reçu {dim}.")
        self.dim: int = dim
        # `default_model` reflète la dim pour que les logs + persistance
        # `embedding_model` soient auto-documentés.
        self.default_model: str = f"mock-{dim}"

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        task_type: str | None = None,  # ignoré (Mock n'a pas de notion DOC vs QUERY)
    ) -> EmbeddingsResponse:
        if not texts:
            raise EmbeddingsInvalidRequestError(
                "Liste de textes vide pour embed().", provider=self.name
            )

        vectors: list[EmbeddingVector] = []
        total_tokens = 0
        effective_model = model or self.default_model
        for text in texts:
            vec = _sha_to_unit_vector(text, self.dim)
            vectors.append(
                EmbeddingVector(
                    values=vec,
                    dim=self.dim,
                    model=effective_model,
                )
            )
            # Heuristique tokens ≈ len/4 (approximation grossière OpenAI
            # `~1 token = 4 chars` pour l'anglais — surestimation pour
            # le français mais suffisant pour la facturation mock).
            total_tokens += max(1, len(text) // 4)

        log.debug(
            "embeddings.mock.done",
            count=len(vectors),
            total_tokens=total_tokens,
            dim=self.dim,
        )
        return EmbeddingsResponse(
            vectors=vectors,
            usage=EmbeddingsUsage(prompt_tokens=total_tokens, total_tokens=total_tokens),
        )


def _sha_to_unit_vector(text: str, dim: int) -> list[float]:
    """Transforme un texte en vecteur déterministe de `dim` floats normés L2.

    Pipeline :
    1. SHA-256 UTF-8 → 32 bytes.
    2. Étire par répétition jusqu'à >= `dim` bytes (ceil(dim/32) copies).
    3. Centre : `(byte - 127.5) / 127.5` → [-1, 1].
    4. Tronque à `dim` valeurs.
    5. Normalise L2 → ||v|| = 1.

    **Note** : comme on répète le même digest tel quel, le vecteur final
    a une structure périodique (sous-blocs identiques de 32). En test
    réel ça ne pose aucun problème — on vérifie juste que deux textes
    différents produisent deux vecteurs différents, et que la forme SQL
    de la recherche cosinus fonctionne. Pour une vraie sémantique, le
    provider réel (Gemini 768, OpenAI 1536) prend le relais.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()  # 32 bytes
    repeats = (dim + len(digest) - 1) // len(digest)
    stretched = (digest * repeats)[:dim]
    centered = [(b - 127.5) / 127.5 for b in stretched]
    norm = math.sqrt(sum(x * x for x in centered))
    if norm == 0.0:
        # Garde-fou théorique : retourne tel quel (pas normé).
        return centered
    return [x / norm for x in centered]
