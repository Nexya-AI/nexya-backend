"""Feature Library — bibliothèque personnelle de l'utilisateur.

Stocke les images générées par l'IA (auto-save depuis `/image/generate`),
les fichiers uploadés futurs (session E3), les audios et vidéos produits.
Expose un CRUD paginé avec filtres combinables (type, source,
conversation_id, q trigram sur titre).

Binaire hébergé sur MinIO/S3 via le wrapper `core/storage/object_store.py`
(mock-first pour dev sans container). Client Flutter reçoit des presigned
URLs MinIO générées à la volée (TTL 1 h) — pas de proxy applicatif.
"""
