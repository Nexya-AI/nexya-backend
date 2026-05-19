"""
NEXYA — Package des system prompts experts affûtés au niveau divin
(Session A2, Période 2 IA-Quality Phase A, 2026-05-19).

Chaque expert NEXYA (general + 10 spécialisés) a son module dédié dans ce
package, exposant une constante `SYSTEM_PROMPT: str` consommée par
`app/ai/experts.py` au moment de la construction du registre `EXPERT_REGISTRY`.

Pourquoi un package dédié et pas tout dans `experts.py` ?

1. **Lisibilité** — chaque prompt expert fait 50-150 lignes ciselées. Tout
   regrouper dans `experts.py` ferait exploser le fichier à 2000+ lignes.
2. **Tests grow naturellement** — un fichier de test par expert
   (`test_expert_prompts_X.py`) qui peut s'enrichir au fil des versions
   sans polluer le test du registre.
3. **Évolutions ciblées** — refondre le prompt cooking n'impacte que
   `cooking.py`, pas les 10 autres experts.
4. **Pattern Silicon Valley standard** — séparation concerns, single
   responsibility, ouvert à l'extension sans modifier l'existant.

Architecture canonique d'un module expert :

  - Docstring décrivant la philosophie du prompt + sources d'inspiration
  - Imports depuis `app.ai.expert_prompts._shared` (constantes + clauses
    transverses + dataclass `FewShotExample` + helper
    `format_few_shot_examples` + `build_system_prompt`)
  - 4 constantes internes typées `Final[str]` :
      * `_PERSONA`           (L1 — persona profonde)
      * `_METHODOLOGY`       (L2 — méthodologie step-by-step)
      * `_OUTPUT_TEMPLATES`  (L3 — N templates de sortie calibrés)
      * `_ANTI_PATTERNS`     (L5 — comportements interdits explicites)
  - Tuple `_FEW_SHOT_EXAMPLES: Final[tuple[FewShotExample, ...]]` (L4)
  - Constante publique `SYSTEM_PROMPT: Final[str]` assemblée via
    `build_system_prompt(...)` du `_shared` (ordre canonique figé A2 :
    persona → methodology → templates → few-shot → anti-patterns → extras
    → clauses transverses).

Les helpers `_shared.py` produisent des clauses transverses identiques pour
tous les experts (multi-langue dynamique, memory-aware, format markdown).
Le guardrail `_DOMAIN_GUARDRAIL_TEMPLATE` reste appliqué dans `experts.py`
via `_with_guardrail(SYSTEM_PROMPT, ...)` — single point of guardrail
application, on ne l'inclut PAS dans chaque module expert.

L'identité NEXYA + ton + routing est injectée EN AMONT par le préambule
`app/ai/nexya_preamble.py` via le wiring `_stream_link` (Session A1).
Les prompts experts ici sont donc purement **métier** : persona spécialisée
+ méthodologie + templates + anti-patterns. Pas de redondance avec le
preamble (qui contient déjà tone + identité fondateur + routing cross-expert).
"""

from __future__ import annotations

from app.ai.expert_prompts.computer import SYSTEM_PROMPT as COMPUTER_PROMPT
from app.ai.expert_prompts.cooking import SYSTEM_PROMPT as COOKING_PROMPT
from app.ai.expert_prompts.engineering import SYSTEM_PROMPT as ENGINEERING_PROMPT
from app.ai.expert_prompts.finance import SYSTEM_PROMPT as FINANCE_PROMPT
from app.ai.expert_prompts.general import SYSTEM_PROMPT as GENERAL_PROMPT
from app.ai.expert_prompts.language import SYSTEM_PROMPT as LANGUAGE_PROMPT
from app.ai.expert_prompts.legal import SYSTEM_PROMPT as LEGAL_PROMPT
from app.ai.expert_prompts.medicine import SYSTEM_PROMPT as MEDICINE_PROMPT
from app.ai.expert_prompts.productivity import SYSTEM_PROMPT as PRODUCTIVITY_PROMPT
from app.ai.expert_prompts.science import SYSTEM_PROMPT as SCIENCE_PROMPT
from app.ai.expert_prompts.studio import SYSTEM_PROMPT as STUDIO_PROMPT

__all__ = (
    "COMPUTER_PROMPT",
    "COOKING_PROMPT",
    "ENGINEERING_PROMPT",
    "FINANCE_PROMPT",
    "GENERAL_PROMPT",
    "LANGUAGE_PROMPT",
    "LEGAL_PROMPT",
    "MEDICINE_PROMPT",
    "PRODUCTIVITY_PROMPT",
    "SCIENCE_PROMPT",
    "STUDIO_PROMPT",
)
