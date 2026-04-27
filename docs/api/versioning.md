# API Versioning Policy — NEXYA Backend

> **Executive summary (EN).** V1 = unprefixed paths (`/auth/login`,
> `/chat/stream`). V2 = prefix `/v2/*` introduced ONLY when a breaking
> change is unavoidable. Backward-compat default. Deprecation timeline
> placeholder.

---

## V1 actuel — unprefixed

Tous les endpoints actuels sont **non préfixés** :

```
✅ POST /auth/login
✅ POST /chat/stream
✅ GET /healthz
```

**Pas de `/v1/*`** — économie de path et compatibilité Flutter
(évite la complexité initiale d'un router dédié).

---

## Quand bumper vers `/v2/*`

V2 ne doit être introduit **que** si une de ces 3 conditions est
remplie :

1. **Breaking change body request/response** d'un endpoint critique
   utilisé en prod par Flutter
   (ex: renommer `data.db` string → dict — déjà fait O1, mais documenté
   comme breaking attendu, pas un bump V2)
2. **Changement security scheme** (ex: passage Bearer JWT → mTLS,
   passage RS256 → ES256)
3. **Suppression d'un endpoint** non remplaçable par déprecation
   sémantique

**N'est PAS un breaking change** :
- Ajouter un nouveau champ optionnel dans la response
- Ajouter un nouveau endpoint
- Élargir la valeur d'un Literal (ajouter une option)
- Améliorer un message d'erreur (le `code` reste stable)

---

## Stratégie de migration V1 → V2

Quand V2 est introduit :

```
1. Ajouter les nouveaux endpoints sous /v2/* en parallèle
2. V1 et V2 cohabitent pendant 6 mois minimum
3. Header de réponse Deprecation:
   Deprecation: true
   Sunset: 2027-04-01
   Link: <https://nexya.ai/api-changelog>; rel="deprecation"
4. Notification email aux developers via support
5. Suppression V1 après 6+ mois
```

---

## Deprecation policy

Aucun endpoint actuel V1 n'est encore déprécié au 2026-04-27.

Quand un endpoint sera déprécié :

1. **Annonce** dans `docs/api/changelog.md` (V2 — pas encore créé)
2. **Header HTTP** `Deprecation: true` + `Sunset: <ISO date>` posé
   par middleware
3. **Body response** ajout `_deprecation_warning` field
4. **Email** aux integrators (V2 — quand on aura un programme dev)
5. **Suppression** ≥ 6 mois après annonce

---

## Versioning OpenAPI vs URL

OpenAPI a une `info.version` (`0.1.0` actuel) qui suit le **semver
applicatif** :
- MAJOR : breaking change majeur (rare)
- MINOR : nouvel endpoint / feature (fréquent)
- PATCH : bug fix sans changement contrat

Le **path versioning** (`/v2/*`) n'est utilisé QUE pour les breaking
changes critiques. Le `info.version` peut bumper sans toucher aux
paths.

Exemple :
- `0.4.2` → `0.5.0` : ajout endpoint `/voice/stream` (MINOR, pas de
  changement path versioning)
- `0.5.0` → `1.0.0` : breaking change auth scheme → bump path V2 +
  bump major

---

## Compatibilité Flutter

Le datasource Dart consomme V1 unprefixed. Quand on introduira V2 :

1. Datasource V1 + V2 cohabiteront temporairement
2. Migration progressive endpoint par endpoint via feature flags
   server-side
3. Build Flutter minSDK pour forcer mise à jour app post-deprecation
   sunset date

---

## TODO V2 (post-launch)

- Créer `docs/api/changelog.md` (Keep a Changelog format)
- Implémenter middleware deprecation header (V2 si besoin)
- Programme integrator (clés API v2 + portail dev) — Phase post-launch
- Webhook deprecation notifications email — Phase post-launch
