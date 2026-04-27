# Évals IA — NEXYA Backend (Session N3)

Harness reproductible d'évaluation qualitative des réponses NEXYA.
Détecte automatiquement les régressions de qualité IA introduites par
un PR (changement prompt système, modèle, fallback chain, SDK provider)
avant qu'elles n'atteignent la prod.

## 🎯 Ce que ça teste

| Catégorie | Quoi | Comment | Seuil pass |
|---|---|---|---|
| `routing` | Contrat `LlmRouter` (`expert_id` → primary_provider/model) | Pure introspection de `EXPERT_REGISTRY` (pas d'appel LLM) | 7.0 |
| `safety` | Refus prescriptions/actes nominatifs + jailbreaks | LLM judge sur la réponse | 8.0+ |
| `format` | Code blocks, LaTeX, listes numérotées | LLM judge | 7.0 |
| `accuracy` | Faits vérifiables par expert | LLM judge | 7.0 |
| `identity` | Marque NEXYA jamais cassée (« je suis Gemini » → fail) | LLM judge | 8.0+ |

Total V1 : **~135 prompts** (15 routing + 28 safety + 30 format + 44 accuracy + 18 identity).

## 🚀 Lancer en local

```bash
# Mock judge (gratuit, déterministe, idéal en dev)
python -m tests.evals --judge=mock --category=all

# Vrai juge Gemini 2.5 Pro (coûte des tokens, qualité réelle)
python -m tests.evals --judge=gemini --category=all

# Juste safety, limite à 5 questions
python -m tests.evals --judge=gemini --category=safety --limit=5

# Update baseline après une amélioration intentionnelle
python -m tests.evals --judge=gemini --update-baseline
```

Rapports écrits dans `tests/evals/reports/report_<date>.md` + `.json` si `--json-out`.

## 📋 Ajouter une nouvelle question

1. Ouvre le YAML de la catégorie : `tests/evals/corpus/<category>.yaml`
2. Ajoute un bloc :

```yaml
- id: format_code_NN          # unique, format <category>_<topic>_<NN>
  expert_id: computer          # null si test cross-expert
  question: "Ta question..."
  expected_pass_score: 7.0     # optionnel, défaut 7.0
  expected_criteria:
    - "Critère 1 vérifiable"
    - "Critère 2 vérifiable"
```

3. Lance localement : `python -m tests.evals --judge=mock --category=<category>`
4. Si pertinent, update la baseline : `--update-baseline`
5. Commit le YAML + le `baseline.json`.

## 🔄 Mettre à jour la baseline

La baseline est gelée dans `tests/evals/baselines/baseline.json` et
committée. Elle doit être actualisée dans **deux cas seulement** :

1. **Première initialisation** (premier run du harness).
2. **Amélioration intentionnelle vérifiée** (refactor prompt qui gagne
   +5pp avec un vrai juge → on fige le nouveau standard).

**Anti-pattern** : pousser la baseline pour faire passer un PR qui
régresse. Le test perd toute sa valeur.

## 🤖 Comment intervenir sur une régression CI

1. **PR mock judge fail** : le mock détecte un changement structurel
   (nouveau YAML, runner cassé). Lis le rapport markdown généré dans
   les artifacts CI. Probablement un YAML malformé ou une catégorie
   nouvelle non baselinée.
2. **Nightly real judge fail** : ouvre l'issue auto. Probablement un
   changement de prompt système ou de modèle qui dégrade la qualité.
   Investigue les questions régressées (top 10 dans le rapport).

## 🧠 Comment ça marche en interne

```
┌──────────────────────────────────┐
│   tests/evals/corpus/*.yaml      │
│   (5 catégories × ~30 prompts)   │
└──────────────┬───────────────────┘
               │ load_corpus()
               ▼
┌──────────────────────────────────┐
│   runner.py::run_evals()         │
└──────────────┬───────────────────┘
               │
               │ pour chaque question :
               ├──► candidate.py
               │    (Gemini SDK direct, system_prompt expert)
               │
               ├──► judge.py
               │    (MockJudge ou GeminiJudge)
               │
               └──► QuestionResult(verdict)
               │
               ▼
┌──────────────────────────────────┐
│   report.py + baseline.py        │
│   - markdown report              │
│   - JSON report                  │
│   - diff vs baseline.json        │
└──────────────────────────────────┘
```

## ❌ Hors scope V1 (différé V2)

- MMLU/HellaSwag/BIG-Bench complets (trop lent, trop cher)
- Perplexité (Gemini SDK n'expose pas les logprobs)
- Multi-juge ensemble (Claude + Gemini)
- A/B test prod traffic (post-launch)
- Auto-publish leaderboard
- Évals images/audio (text-only V1)
- Cache des réponses LLM (chaque run = preuve fraîche)
