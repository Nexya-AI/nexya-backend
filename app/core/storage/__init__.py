"""Object storage + file processing — abstractions S3 / MinIO / R2 (C3),
détection MIME magic-bytes, extraction texte PDF/DOCX, scan virus (E3)."""

from app.core.storage.mime_detector import detect_mime_type, mimes_compatible
from app.core.storage.object_store import (
    MockObjectStore,
    ObjectStat,
    ObjectStore,
    ObjectStoreUnavailableException,
    S3ObjectStore,
    get_object_store,
    reset_object_store,
)
from app.core.storage.text_extractor import (
    ExtractedText,
    ExtractionStatus,
    extract_text,
)
from app.core.storage.virus_scanner import (
    ClamAVScanner,
    MockVirusScanner,
    NoOpVirusScanner,
    ScanResult,
    VirusScanner,
    VirusScanStatus,
    get_virus_scanner,
    reset_virus_scanner,
)

__all__ = [
    # Object store (C3)
    "MockObjectStore",
    "ObjectStat",
    "ObjectStore",
    "ObjectStoreUnavailableException",
    "S3ObjectStore",
    "get_object_store",
    "reset_object_store",
    # MIME detector (E3)
    "detect_mime_type",
    "mimes_compatible",
    # Text extractor (E3)
    "ExtractedText",
    "ExtractionStatus",
    "extract_text",
    # Virus scanner (E3)
    "ClamAVScanner",
    "MockVirusScanner",
    "NoOpVirusScanner",
    "ScanResult",
    "VirusScanner",
    "VirusScanStatus",
    "get_virus_scanner",
    "reset_virus_scanner",
]
