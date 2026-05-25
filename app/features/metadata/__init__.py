"""Feature Metadata (Session C4.2 — 2026-05-24).

URL preview cards : endpoint `POST /metadata/url-preview {url}` qui fetch
les Open Graph tags d'une URL (title/description/image/favicon) côté serveur
avec cache Redis 7j + anti-SSRF strict + rate limit user-scope 60/h.

Le frontend Flutter consomme ce endpoint pour afficher des cartes preview
riches dans le chat — économise 1 MB de bande passante 2G/3G Africa-first
(1 fetch backend cached vs 5 fetches client × 200 KB HTML brut).
"""
