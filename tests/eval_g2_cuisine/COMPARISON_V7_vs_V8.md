# Blind test G2 — Comparaison V7 baseline vs V8 chunking

> Lancé le 2026-05-18 sur le poste Ivan via Vertex AI gemini-2.5-pro.
> 30 questions identiques, 2 corpus différents (V7 = 117 chunks tronqués,
> V8 = 192 chunks via chunking par paragraphe).

## Score global : ÉQUIVALENT

| Métrique | V7 baseline | V8 chunked | Δ |
|---|---|---|---|
| Wins A (NEXYA RAG) | **26** | **26** | = |
| Wins B (Gemini brut) | 4 | 4 | = |
| Ties | 0 | 0 | = |
| **% wins A** | **86,7 %** | **86,7 %** | = |
| Pass seuil 60 % | ✅ | ✅ | = |
| Pass objectif 80 % | ✅ | ✅ | = |

**Conclusion globale** : le chunking par paragraphe V8 N'A PAS amélioré le score
mais N'A PAS dégradé non plus. Le gain V8 est **architectural** (corpus propre,
catégories cohérentes, plus de troncature silencieuse, déchets filtrés)
plutôt que statistique sur ce dataset de 30 questions.

## Delta question par question (8 différences)

| ID | Sujet | V7 winner | V7 A/B | V8 winner | V8 A/B | Δ A | Δ B |
|---|---|---|---|---|---|---|---|
| cm_03 | Eru | A | 9.5 / 8.5 | **B** 🔴 | 8.0 / 10.0 | -1.5 | +1.5 |
| cm_05 | Kondre | A | 9.5 / 8.0 | **B** 🔴 | 9.0 / 9.5 | -0.5 | +1.5 |
| cm_14 | Achu | B | 9.5 / 10.0 | **A** 🟢 | 9.5 / 9.0 | = | -1.0 |
| cm_15 | Jus Bissap | B | 8.5 / 9.5 | **A** 🟢 | 9.5 / 7.5 | +1.0 | -2.0 |
| cm_06 | Njama Njama | B | 8.0 / 9.5 | B (=) | 7.5 / 9.0 | -0.5 | -0.5 |
| daily_16 | substitution arachide | B | 4.0 / 9.5 | B (=) | 4.0 / 9.0 | = | -0.5 |
| oos_28 | medical | A 10/6 | = | A 10/7.5 | = | = | +1.5 |
| oos_30 | traduction | A 10/2 | = | A 10/2 | = | = | = |

**Bilan delta** :
- 2 GAINS (cm_14_achu + cm_15_jus_bissap) — le chunking par paragraphe a aidé
  car ces recettes ont plusieurs sous-sections distinctes (Achu = pâte + sauce
  jaune ; Bissap = base infusion + variations).
- 2 LOSSES (cm_03_eru + cm_05_kondre) — le chunking a fragmenté ces recettes
  courtes, le LLM B brut connaît bien ces plats et y répond directement sans
  avoir à reconstruire depuis N chunks RAG.
- Tout le reste stable.

## Insights V1.1+

### Pourquoi `daily_16_substitution` perd 5.5 points (4.0 vs 9.5) en V7 ET V8

Question : *« Je n'ai pas de pâte d'arachide pour mon Ndolé, par quoi je peux la remplacer ? »*

Réponse A NEXYA : **refuse de proposer une alternative** au prétexte de
l'authenticité du corpus camerounais. Refus à tort — l'user demande explicitement
une substitution, c'est légitime.

Root cause : le system prompt `_COOKING_PROMPT` mentionne *« Adapte aux moyens
locaux : si un ingrédient est rare au Cameroun, propose une alternative
accessible »* mais ne dit pas explicitement *« propose toujours une alternative
quand l'user le demande, même si l'ingrédient existe au Cameroun »*.

**Fix V1.1** : ajouter au prompt cooking une instruction claire :
*« Quand l'user demande une substitution, propose toujours au moins 2 alternatives
réalistes avec leur ratio, même si l'ingrédient original existe localement. »*

### Pourquoi `cm_06_njama_njama` perd en V7 ET V8

Le RAG retrouve un chunk Njama Njama mais qui n'est pas optimal (probablement
le bloc fusionné « KATI KATI & NJAMA NJAMA » du vol 1.2). Le LLM B brut connaît
mieux cette feuille verte camerounaise et répond plus précisément.

**Fix V1.1** : isoler la recette Njama Njama du bloc fusionné via
`_split_inline_subrecipe()` (P2.2 du runbook).

### Pourquoi cm_03_eru et cm_05_kondre régressent en V8

Le chunking a séparé ces recettes en plusieurs chunks. Quand le RAG retourne
top-5 chunks, plusieurs chunks de la même recette occupent les slots, ce qui
réduit la diversité informationnelle. Le LLM A voit la même recette 3× mais
manque d'info contextuelle, alors que le LLM B brut a directement la réponse
complète sans fragmentation.

**Fix V1.1** : ajouter au retrieval une stratégie de **dédup par `id_slug`** :
si plusieurs chunks de la même recette sont dans le top-K, ne garder que le
chunk de plus haut score. Permet de diversifier les sources.

```python
# Dans ExpertCorpusService.search ou _format_corpus_block :
seen_slugs = set()
deduplicated = []
for chunk in chunks:
    slug = chunk.metadata.get("id_slug")
    if slug and slug in seen_slugs:
        continue
    seen_slugs.add(slug)
    deduplicated.append(chunk)
```

## Décision finale G2 V8

Le score 26/30 = 86,7 % reste excellent et au-dessus de l'objectif Silicon
Valley senior (80 %). Le chunking par paragraphe est conservé pour ses
bénéfices architecturaux. Les 4 LOSS sont documentés et listés en backlog
V1.1.

**G2 V8 = SHIP IT.** Pas de blocker pour activer cooking en production.

Prochain RAG à activer : G4 Ingénierie ou G6 Informatique (voir CLAUDE.md §15
journal). Réutilisable le même pattern parser + ingest + blind test.
