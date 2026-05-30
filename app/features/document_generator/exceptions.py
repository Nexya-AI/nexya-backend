"""Exceptions sealed pour document_generator (C4.7a).

Pattern strict aligné `app/core/errors/exceptions.py` et les autres
features NEXYA (rendering, metadata, library, files) : chaque erreur
porte un `code` stable mappé côté frontend Flutter pour dispatch
typé snackbar.

Hiérarchie :
    DocumentGeneratorError (base, non-instanciée directement)
    ├── TemplateNotFoundError           # 422 — template invalide
    ├── DocumentSourceTooLongError      # 413 — source markdown > cap chars
    ├── DocumentRenderFailedError       # 503 — WeasyPrint timeout/crash
    ├── DocumentTruncatedError          # 200 mais flag truncated=True (info, pas raise)
    └── DocumentStorageUnavailableError # 503 — MinIO upload échoué
"""

from __future__ import annotations


class DocumentGeneratorError(Exception):
    """Base sealed pour toutes les erreurs document_generator.

    Ne JAMAIS être levée directement — utiliser les sous-classes
    spécifiques pour dispatch côté router (mapping `code` → HTTP status).
    """

    code: str = "DOCUMENT_GENERATOR_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class TemplateNotFoundError(DocumentGeneratorError):
    """Template demandé n'existe pas (422).

    Levée si le client envoie un slug template qui n'existe pas dans
    `TEMPLATE_REGISTRY`. En pratique Pydantic Literal devrait empêcher
    ce cas avant qu'on arrive ici — défense en profondeur.
    """

    code = "TEMPLATE_NOT_FOUND"


class DocumentSourceTooLongError(DocumentGeneratorError):
    """Source markdown dépasse le cap configurable (413).

    Cap V1 = 200 000 caractères (~50 pages worst-case). Au-delà,
    WeasyPrint risque de timer out + le PDF résultant serait trop gros.
    Le client (Flutter) doit tronquer côté UI AVANT envoi ou afficher
    un message « document trop long, scinde en plusieurs parties ».
    """

    code = "DOCUMENT_SOURCE_TOO_LONG"


class DocumentRenderFailedError(DocumentGeneratorError):
    """WeasyPrint crash, timeout 30s, ou subprocess kill (503).

    Causes possibles :
        - Dépendances système manquantes (cairo/pango/gdk-pixbuf)
        - HTML/CSS pathologique (boucle infinie de page break)
        - Source markdown malformé qui produit un HTML invalide
        - OOM container (PDF avec 1000 images base64)

    Le router log + retourne 503 fail-safe absolu (le service n'est
    pas critique — l'user peut retenter avec une source plus simple).
    """

    code = "DOCUMENT_RENDER_FAILED"


class DocumentTruncatedError(DocumentGeneratorError):
    """Marker INFO (pas raise normalement) : PDF tronqué au cap pages.

    Utilisé comme valeur de retour `truncated=True` dans la response.
    Si on lève cette exception côté service, c'est pour indiquer un
    flag à exposer côté response — pas pour faire échouer la requête.
    Le client (Flutter) affiche un badge « PDF tronqué à 50 pages ».
    """

    code = "DOCUMENT_TRUNCATED"


class DocumentStorageUnavailableError(DocumentGeneratorError):
    """LibraryService.create_from_bytes a échoué (503).

    Causes : MinIO down, quota Library Pro atteint, exception network.
    Le PDF est rendu mais on ne peut pas le persister.
    Fail-safe côté router : log warning + return 503.
    """

    code = "DOCUMENT_STORAGE_UNAVAILABLE"
