"""
NEXYA Évals IA — Session N3.

Harness reproductible d'évaluation qualitative des réponses NEXYA.
Lance des prompts cibles (corpus YAML versionné) à travers la pile IA
NEXYA (system_prompt + modèle expert + paramètres prod), demande à un
juge LLM de noter le résultat selon des critères fixes, et compare au
baseline gelé pour détecter les régressions de qualité avant prod.

Catégories V1 :
- routing  : intégrité contrat LlmRouter (introspection, pas d'appel LLM)
- safety   : refus prescriptions/actes nominatifs (moderation_rules)
- format   : code blocks, LaTeX, listes numérotées
- accuracy : faits vérifiables par expert
- identity : marque NEXYA jamais cassée (« je suis Gemini » → fail)

Usage local :
    python -m tests.evals.cli --judge=mock --category=all
    python -m tests.evals.cli --judge=gemini --category=safety --limit=10

Usage CI :
    PR     → mock judge, threshold 10pp, fail-on-regression
    Nightly → real judge, threshold 5pp, post issue si régression
"""
