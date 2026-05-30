"""Feature Document Generator (Session C4.7a — 2026-05-30).

Génération de documents PDF premium via WeasyPrint pour la catégorie D
de la roadmap C4 (Documents pro générés). L'utilisateur upload un PDF
brut + demande une transformation via un mode expert (Sciences/Legal/
Medicine/Cooking/Business), le backend rend un PDF stylisé avec un
template Jinja2 + WeasyPrint (cairo/pango natifs).

Endpoint :
    POST /generate/document
        body : DocumentGenerateRequest{conversation_id, message_id,
            format=pdf, template ∈ {school, minimal}, options{},
            title?}
        retour : DocumentGenerateResponse{library_id, download_url,
            filename, size_bytes, pages, truncated, expires_at,
            generated_at}

Pipeline (côté service) :
    1. Validation Pydantic (template ∈ Literal, format=pdf V1, title cap 200)
    2. Récupération source markdown depuis le message (max 200k chars)
    3. Rendu Jinja2 sandbox via template choisi (school/minimal)
    4. WeasyPrint HTML+CSS → PDF avec timeout 30s + cap 50 pages
    5. Post-process pikepdf (compression streams + metadata)
    6. Upload MinIO via LibraryService.create_from_bytes(type=document, file_type=pdf)
    7. Presigned URL TTL 30 min retournée au client

Sécurité :
    - Anti path traversal STRICT : template via Literal enum (jamais raw string)
    - Sandbox WeasyPrint : subprocess timeout 30s, cap 50 pages, 200 KB source max
    - Pas d'URL fetching externe (images locales en base64 uniquement V1)
    - Sealed exceptions : DocumentRenderFailedError, DocumentSourceTooLongError,
      TemplateNotFoundError, DocumentTruncatedError

Caps Africa-first :
    - 50 MB ZIP max (futur DOCX C4.7b)
    - 50 fichiers max par projet utilisateur
    - 100k chars/fichier source markdown
    - Cap 50 pages PDF (truncated=True si dépassement)

V2 différé :
    - Format DOCX via python-docx (C4.7b)
    - 5 templates supplémentaires (Sciences, Legal, Medicine, Cooking, Business)
    - Watermark NEXYA branding optionnel
    - C2PA signing (AI Act août 2026)
"""
