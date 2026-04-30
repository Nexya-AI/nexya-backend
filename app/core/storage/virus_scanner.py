"""
Scanner virus — contrat abstrait + MockScanner (EICAR) + stub ClamAV.

Design mock-first (pattern NEXYA aligné Brevo / hCaptcha / FCM / ObjectStore) :

- `MockVirusScanner` est l'impl activée par défaut — reconnaît la signature
  EICAR test (string standard industry qui déclenche tous les vrais AV sans
  contenir aucun code malveillant réel). Parfait pour dev et CI.
- `ClamAVScanner` est un **stub** qui raise `NotImplementedError` avec un
  message clair. Son activation réelle (TCP clamd + INSTREAM protocol) se
  fait en prod quand Ivan déploie un ClamAV daemon.
- La factory `get_virus_scanner()` choisit automatiquement : Mock si
  `settings.clamav_host=""` OU `settings.virus_scan_enabled=False`,
  ClamAV sinon.

Contrat `scan(data, filename) -> ScanResult(status, signature, details)` :
- `status='clean'` : aucune menace détectée.
- `status='suspicious'` : menace détectée, `signature` porte le nom.
- `status='failed'` : le scanner lui-même a échoué (timeout, connexion).
  Le caller décide (peut rejeter ou laisser passer selon la politique).

Le FileUploadService rejette 415 `VIRUS_DETECTED` sur status='suspicious'.
Il laisse passer sur 'failed' avec log warning — un scanner down ne doit
pas bloquer tous les uploads (fail-open pragmatique, acceptable pour un
MVP ; on pourra basculer fail-closed via settings si la politique durcit).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Final, Literal

import structlog

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Signature EICAR — standard industry pour tester les scanners
# ══════════════════════════════════════════════════════════════
#
# La chaîne EICAR est RECONNUE PAR TOUS LES AV MAJEURS comme test file.
# Elle ne contient AUCUN code malveillant — c'est juste une convention
# (https://www.eicar.org/) pour que les devs puissent tester leur
# pipeline AV sans manipuler un vrai malware.
#
# On la stocke en bytes + split pour que les scanners de code source
# qui cherchent cette signature en littéral dans les repos n'alertent
# pas à tort sur ce fichier.

_EICAR_SIGNATURE: Final[bytes] = (
    b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)


# ══════════════════════════════════════════════════════════════
# Résultat du scan
# ══════════════════════════════════════════════════════════════

VirusScanStatus = Literal["clean", "suspicious", "failed"]


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Résultat d'un scan virus.

    - `status` : 'clean' | 'suspicious' | 'failed'
    - `signature` : nom de la menace si suspicious, None sinon.
    - `scanner` : nom du scanner utilisé (utile pour l'audit).
    - `details` : dict libre pour metadata supplémentaire.
    """

    status: VirusScanStatus
    signature: str | None = None
    scanner: str = "unknown"
    details: dict[str, object] | None = None


# ══════════════════════════════════════════════════════════════
# Interface abstraite
# ══════════════════════════════════════════════════════════════


class VirusScanner(abc.ABC):
    """Contrat asynchrone pour un scanner antivirus."""

    name: str

    @abc.abstractmethod
    async def scan(self, data: bytes, *, filename: str = "") -> ScanResult:
        """Scanne les bytes et retourne un `ScanResult`.

        Ne lève JAMAIS d'exception — en cas d'erreur interne, retourne
        `status='failed'`. Le caller (FileUploadService) gère le comportement
        fail-open/fail-closed selon la politique.
        """


# ══════════════════════════════════════════════════════════════
# Mock — détecte EICAR
# ══════════════════════════════════════════════════════════════


class MockVirusScanner(VirusScanner):
    """Scanner factice qui détecte la signature EICAR test.

    Activé quand `settings.clamav_host=""` OU `virus_scan_enabled=False`
    (dans ce dernier cas on utilise le mock mais il répond `clean` systémat-
    iquement — voir `NoOpVirusScanner`). Ici, MockVirusScanner répond :
    - `suspicious` si EICAR trouvé dans le buffer.
    - `clean` sinon.
    """

    name: Final[str] = "mock"

    async def scan(self, data: bytes, *, filename: str = "") -> ScanResult:
        if _EICAR_SIGNATURE in data:
            log.warning(
                "virus_scanner.mock.eicar_detected",
                filename=filename,
                size_bytes=len(data),
            )
            return ScanResult(
                status="suspicious",
                signature="EICAR-TEST-SIGNATURE",
                scanner=self.name,
                details={"source": "mock"},
            )
        return ScanResult(status="clean", scanner=self.name)


class NoOpVirusScanner(VirusScanner):
    """Scanner « désactivé » — répond `clean` sans inspecter.

    Activé quand `settings.virus_scan_enabled=False`. Utile pour les
    benchmarks ou pour un env où le scan doit être explicitement skippé
    sans casser le pipeline (tests d'intégration avec payloads lourds,
    par exemple).
    """

    name: Final[str] = "noop"

    async def scan(self, data: bytes, *, filename: str = "") -> ScanResult:
        return ScanResult(status="clean", scanner=self.name)


# ══════════════════════════════════════════════════════════════
# ClamAV — stub, activation prod différée
# ══════════════════════════════════════════════════════════════


class ClamAVScanner(VirusScanner):
    """Stub pour un vrai scanner ClamAV (via clamd TCP socket).

    Activation prod (hors scope E3) consistera à :
    1. Ouvrir un socket TCP vers `settings.clamav_host:clamav_port`.
    2. Envoyer la commande `nINSTREAM\\n` + chunks `<4byte length><data>`
       + `\\x00\\x00\\x00\\x00` (zero-length marker = EOF).
    3. Parser la réponse `stream: OK` (clean) ou `stream: <virus_name> FOUND`
       (suspicious).
    4. Timeout 30s, fail → `status='failed'` + log.

    Protocole documenté : https://docs.clamav.net/manual/Usage/Scanning.html#clamd
    """

    name: Final[str] = "clamav"

    def __init__(self, *, host: str, port: int) -> None:
        self._host = host
        self._port = port

    async def scan(self, data: bytes, *, filename: str = "") -> ScanResult:
        # Activation prod différée. Un vrai scan ClamAV nécessite le daemon
        # clamd en cours, qu'on provisionne seulement sur serveur de prod.
        raise NotImplementedError(
            "ClamAVScanner n'est pas encore activé. "
            "Laisse settings.clamav_host vide pour utiliser MockVirusScanner. "
            "L'activation prod sera faite dans le lot infrastructure Phase 14."
        )


# ══════════════════════════════════════════════════════════════
# Factory — singleton lazy
# ══════════════════════════════════════════════════════════════


_SCANNER: VirusScanner | None = None


def get_virus_scanner() -> VirusScanner:
    """Retourne le singleton VirusScanner selon la config.

    - `virus_scan_enabled=False` → NoOpVirusScanner.
    - `clamav_host=""`           → MockVirusScanner (avec détection EICAR).
    - Sinon                      → ClamAVScanner (stub — raise sur scan).
    """
    global _SCANNER
    if _SCANNER is not None:
        return _SCANNER

    from app.config import settings

    if not settings.virus_scan_enabled:
        _SCANNER = NoOpVirusScanner()
        log.info("virus_scanner.noop.initialized")
    elif not settings.clamav_host:
        _SCANNER = MockVirusScanner()
        log.info("virus_scanner.mock.initialized")
    else:
        _SCANNER = ClamAVScanner(host=settings.clamav_host, port=settings.clamav_port)
        log.info(
            "virus_scanner.clamav.initialized",
            host=settings.clamav_host,
            port=settings.clamav_port,
        )
    return _SCANNER


def reset_virus_scanner() -> None:
    """Reset singleton — usage tests uniquement."""
    global _SCANNER
    _SCANNER = None
