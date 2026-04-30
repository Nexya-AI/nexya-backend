"""Feature Files — upload utilisateur générique (Session E3).

Pipeline `POST /files/upload` :
MIME annoncé → cap taille → magic-bytes détection → dédup SHA →
scan virus (mock EICAR / ClamAV stub) → upload MinIO → INSERT DB →
extraction texte (PDF pypdf / DOCX zipfile+xml / text plain).

Les uploads sont **buffers polymorphes** consommés en aval par :
- Projects Files (Session C2 → étendu en E3 avec `upload_id`).
- Library (futur — upload base64 déjà livré en C3).
- Memory Documents pour RAG (Session D4 futur).
"""
