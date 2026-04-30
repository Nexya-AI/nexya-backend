"""Feature Memory — mémoire IA long terme (socle Session D1).

Socle DB + service interne. **Aucun endpoint HTTP exposé au D1** — les
endpoints publics `/memory/search`, `/memory/index`, `/memory/list`,
`DELETE /memory/{id}` arriveront en session D5.

Consommé par :
- **D2** (job arq post-conversation) : extraction de 0-3 faits durables
  d'une conv terminée → `MemoryStore.add(source='extracted', ...)`.
- **D3** (hook `/chat/stream`) : `MemoryStore.search(user, query, k=5)`
  avant l'appel LLM → injection dans le system prompt.
- **D5** (RAG endpoint) : exposition publique des endpoints `/memory/*`.
- **Phase I2 Flutter** (UI « Ma mémoire »).
"""
