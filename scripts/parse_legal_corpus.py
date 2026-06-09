"""
Parser article-aware du corpus Expert Juridique (Phase 2, étape 1).

Transforme les 14 textes .md structurés (Bloc 1) en chunks **par article**
prêts pour l'embedding + l'ingestion pgvector, avec métadonnées riches.

Principe senior :
- L'unité atomique = l'ARTICLE (jamais un découpage aveugle tous les N chars).
  Une question juridique vise quasi toujours un article précis.
- Chaque chunk porte une fiche complète : code, domaine, juridiction, année,
  statut, numéro d'article, et le CHEMIN HIÉRARCHIQUE (Partie > Livre > Titre >
  Chapitre > Section). C'est ce qui rend les réponses citables ET désambiguïse
  les COMPILATIONS (Code civil / CGI ont des numéros d'articles qui se répètent).
- Les articles longs (> cap embedding) sont re-découpés par paragraphe, en
  répliquant l'en-tête de contexte sur chaque sous-chunk.

Sortie (aucune écriture DB ici, 100 % hors-ligne) :
- `_canonical_legal/legal_chunks.jsonl` : 1 chunk par ligne (content + metadata).
- `_canonical_legal/_validation_legal.md` : rapport de validation par texte.

Usage :
    python scripts/parse_legal_corpus.py

Les métadonnées de chaque document sont lues à la SOURCE depuis l'en-tête du
.md (`**ref**` + `<!-- meta: ... -->`). Le registre ne fournit que le chemin,
le libellé d'affichage et le tag de source.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# ──────────────────────────────────────────────────────────────────
# Chemins
# ──────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DATASET = _REPO_ROOT.parent / "DATAS SETS" / "Expert Juridique"
_EXTRACTED = _DATASET / "extracted"
_PENAL_DIR = _DATASET / "DROIT ET JUSTICE" / "LOIS ET AUTRES TEXTES" / "DROIT PENAL"

OUT_DIR = _DATASET / "_canonical_legal"
OUT_JSONL = OUT_DIR / "legal_chunks.jsonl"
OUT_REPORT = OUT_DIR / "_validation_legal.md"

EXPERT_SLUG = "legal"

# Cap pratique du provider Gemini text-embedding (2048 chars). Marge de
# sécurité confortable identique au pipeline cuisine.
EMBED_TEXT_CAP = 1700

# ──────────────────────────────────────────────────────────────────
# Registre des 14 documents cœur (V1)
#   slug -> (chemin, libellé d'affichage, tag source)
#   Les autres champs (code/domaine/juridiction/année/statut/note) sont lus
#   depuis la ligne <!-- meta: ... --> du fichier. code_penal n'en a pas :
#   fallback explicite ci-dessous.
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DocCfg:
    slug: str
    path: Path
    label: str
    source_tag: str
    fallback_meta: dict[str, str] = field(default_factory=dict)


REGISTRY: list[DocCfg] = [
    DocCfg(
        "code_penal_cameroun",
        _PENAL_DIR / "code_penal_cameroun.md",
        "Code pénal camerounais",
        "cm-loi-2016-007",
        fallback_meta={
            "code": "CP",
            "domain": "penal",
            "jurisdiction": "CM",
            "year": "2016",
            "status": "in_force",
        },
    ),
    DocCfg(
        "code_procedure_penale_cameroun",
        _EXTRACTED / "code_procedure_penale_cameroun.md",
        "Code de procédure pénale camerounais",
        "cm-loi-2005-007",
    ),
    DocCfg(
        "decret_reglementaire_code_penal",
        _EXTRACTED / "decret_reglementaire_code_penal.md",
        "Partie réglementaire du Code pénal (contraventions)",
        "cm-decret-2016-319",
    ),
    DocCfg(
        "loi_cybersecurite_cybercriminalite_cameroun",
        _EXTRACTED / "loi_cybersecurite_cybercriminalite_cameroun.md",
        "Loi sur la cybersécurité et la cybercriminalité",
        "cm-loi-2010-012",
    ),
    DocCfg(
        "ohada_societes_commerciales_gie",
        _EXTRACTED / "ohada_societes_commerciales_gie.md",
        "Acte uniforme OHADA sur les sociétés commerciales et le GIE",
        "ohada-auscgie-2014",
    ),
    DocCfg(
        "ohada_droit_commercial_general",
        _EXTRACTED / "ohada_droit_commercial_general.md",
        "Acte uniforme OHADA sur le droit commercial général",
        "ohada-audcg",
    ),
    DocCfg(
        "cima_code_assurances",
        _EXTRACTED / "cima_code_assurances.md",
        "Code CIMA des assurances",
        "cima-traite-1992",
    ),
    DocCfg(
        "code_travail_cameroun",
        _EXTRACTED / "code_travail_cameroun.md",
        "Code du travail camerounais",
        "cm-loi-92-007",
    ),
    DocCfg(
        "loi_commerce_cameroun",
        _EXTRACTED / "loi_commerce_cameroun.md",
        "Loi régissant l'activité commerciale au Cameroun",
        "cm-loi-90-031",
    ),
    DocCfg(
        "loi_semenciere_cameroun",
        _EXTRACTED / "loi_semenciere_cameroun.md",
        "Loi relative à l'activité semencière",
        "cm-loi-2001-014",
    ),
    DocCfg(
        "ordonnance_regime_foncier_cameroun",
        _EXTRACTED / "ordonnance_regime_foncier_cameroun.md",
        "Ordonnance fixant le régime foncier",
        "cm-ord-74-1",
    ),
    DocCfg(
        "code_civil_cameroun",
        _EXTRACTED / "code_civil_cameroun.md",
        "Code civil applicable au Cameroun",
        "cm-code-civil",
    ),
    DocCfg(
        "cgi_2024_cameroun",
        _EXTRACTED / "cgi_2024_cameroun.md",
        "Code général des impôts (édition 2024)",
        "cm-cgi-2024",
    ),
    DocCfg(
        "loi_droit_auteur_cameroun",
        _EXTRACTED / "loi_droit_auteur_cameroun.md",
        "Loi sur le droit d'auteur et les droits voisins",
        "cm-loi-2000-011",
    ),
]

# ──────────────────────────────────────────────────────────────────
# Détecteurs
# ──────────────────────────────────────────────────────────────────

REF_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")
META_RE = re.compile(r"^<!--\s*meta:\s*(.+?)\s*-->\s*$")
PAGE_RE = re.compile(r"^<!--\s*page\s+(\d+)\s*-->\s*$")
COMMENT_RE = re.compile(r"^<!--.*-->\s*$")

# Divisions : on lit le MOT-CLÉ (pas le niveau #), car PARTIE et LIVRE sont
# tous deux émis en `#` mais PARTIE prime sur LIVRE.
DIV_RE = re.compile(
    r"^#{1,4}\s+(PARTIE|LIVRE|TITRE|CHAPITRE|CHAP|SECTION|SECT|SOUS-SECTION|PARAGRAPHE)\s+"
    r"(.+?)\s*$"
)
ART_RE = re.compile(r"^#{5}\s+ARTICLE\s+(.+?)\s*$")

# Ordinaux latins de sous-articles (bis, ter, ... decies) — fréquents en droit
# fiscal et assurances.
_LATIN = "bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies"
_LATIN_RE = re.compile(rf"(?:{_LATIN})", re.I)

# Rang hiérarchique (slot) par mot-clé.
RANK = {
    "PARTIE": 0,
    "LIVRE": 1,
    "TITRE": 2,
    "CHAPITRE": 3,
    "CHAP": 3,
    "SECTION": 4,
    "SECT": 4,
    "SOUS-SECTION": 4,
    "PARAGRAPHE": 4,
}
SLOT_NAME = ["Partie", "Livre", "Titre", "Chapitre", "Section"]


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def parse_meta(line: str) -> dict[str, str]:
    """`code=CP, domain=penal, ...` -> dict."""
    out: dict[str, str] = {}
    for pair in line.split(","):
        if "=" in pair:
            k, _, v = pair.partition("=")
            out[k.strip().lower()] = v.strip()
    return out


def split_kw_value(rest: str) -> tuple[str, str]:
    """`II — DES PEINES` -> ('II', 'DES PEINES'). `1 — Forme` -> ('1','Forme').
    Gère le séparateur — (tiret cadratin utilisé à l'extraction) ou un simple
    numéro sans libellé."""
    m = re.match(r"^(\S+)\s*[—–-]\s*(.*)$", rest)
    if m:
        return m.group(1), m.group(2).strip()
    return rest.strip(), ""


def base_int(num: str) -> int | None:
    """Numéro d'article -> entier de base pour le contrôle de continuité.
    '275'->275, '18-1'->18, '2-1'->2. Renvoie None si non numérique."""
    m = re.match(r"^(\d+)", num)
    return int(m.group(1)) if m else None


def hierarchy_path(slots: list[str | None]) -> str:
    parts = []
    for name, val in zip(SLOT_NAME, slots):
        if val:
            parts.append(f"{name} {val}")
    return " > ".join(parts)


@dataclass
class Article:
    number: str
    label: str
    page: int
    slots: list[str | None]
    body: str


def chunk_article(art: Article, cfg: DocCfg, ref: str) -> list[str]:
    """Construit le(s) chunk(s) texte d'un article, en répliquant l'en-tête de
    contexte. Découpe par paragraphe si > EMBED_TEXT_CAP."""
    hpath = hierarchy_path(art.slots)
    art_head = f"Article {art.number}"
    if art.label:
        art_head += f" — {art.label}"

    def header(part: str = "") -> str:
        lines = [f"{cfg.label} ({ref})"]
        if hpath:
            lines.append(hpath)
        lines.append(art_head + part)
        return "\n".join(lines) + "\n"

    body = art.body.strip()
    full = header() + body
    if len(full) <= EMBED_TEXT_CAP:
        return [full]

    # Découpe par paragraphe (lignes séparées par blanc) puis packing glouton.
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if not paras:
        paras = [body]
    # budget de corps par chunk (l'en-tête « (partie k/n) » coûte ~12 chars)
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    budget = EMBED_TEXT_CAP - len(header(" (partie 99/99)"))
    for p in paras:
        # paragraphe géant : hard-split sur les phrases puis sur les chars
        pieces = [p]
        if len(p) > budget:
            pieces = re.findall(r".{1," + str(budget) + r"}(?:\s|$)", p) or [p[:budget]]
            pieces = [x.strip() for x in pieces if x.strip()]
        for piece in pieces:
            if cur and cur_len + len(piece) + 2 > budget:
                chunks.append("\n\n".join(cur))
                cur, cur_len = [], 0
            cur.append(piece)
            cur_len += len(piece) + 2
    if cur:
        chunks.append("\n\n".join(cur))

    total = len(chunks)
    return [header(f" (partie {i + 1}/{total})") + c for i, c in enumerate(chunks)]


# ──────────────────────────────────────────────────────────────────
# Parsing d'un document
# ──────────────────────────────────────────────────────────────────


def parse_doc(cfg: DocCfg) -> tuple[list[dict], dict]:
    """Retourne (liste de records chunk, stats du doc)."""
    raw = cfg.path.read_text(encoding="utf-8")
    lines = raw.splitlines()

    ref = ""
    meta: dict[str, str] = {}
    # En-tête : ref (**...**) + meta (<!-- meta: ... -->) dans les ~6 1res lignes
    for ln in lines[:8]:
        if not ref:
            m = REF_RE.match(ln.strip())
            if m:
                ref = m.group(1).strip()
        mm = META_RE.match(ln.strip())
        if mm:
            meta = parse_meta(mm.group(1))
    if not meta:
        meta = dict(cfg.fallback_meta)
    note = meta.get("note", "")

    slots: list[str | None] = [None] * 5
    cur_page = 1
    articles: list[Article] = []
    cur: Article | None = None

    for ln in lines:
        s = ln.rstrip()
        pm = PAGE_RE.match(s.strip())
        if pm:
            cur_page = int(pm.group(1))
            continue
        # Article ?
        am = ART_RE.match(s)
        if am:
            num, label = split_kw_value(am.group(1))
            cur = Article(number=num, label=label, page=cur_page, slots=list(slots), body="")
            articles.append(cur)
            continue
        # Division ?
        dm = DIV_RE.match(s)
        if dm:
            kw = dm.group(1).upper()
            num, label = split_kw_value(dm.group(2))
            r = RANK[kw]
            val = f"{num} — {label}" if label else num
            slots[r] = val
            for j in range(r + 1, 5):
                slots[j] = None
            cur = None  # le texte entre une division et le 1er article = intro
            continue
        # Autre commentaire HTML -> ignore
        if COMMENT_RE.match(s.strip()):
            continue
        # Ligne de corps
        if cur is not None and s.strip():
            cur.body += ("\n" if cur.body else "") + s

    # Construction des chunks + stats
    records: list[dict] = []
    empty_articles: list[str] = []
    multi_chunk = 0
    max_chars = 0
    seen_keys: set[str] = set()
    dup_count = 0

    for art in articles:
        # Normalisation des sous-articles. L'extraction laisse parfois le
        # suffixe d'un sous-article ailleurs que dans le numéro :
        #   - OHADA « ARTICLE 50 — 1 »   -> le « 1 » tombe dans le LABEL
        #   - CGI/CIMA « ARTICLE 18 » + corps « quinquies.- ... » -> dans le CORPS
        # On reconstruit le vrai numéro (« 50-1 », « 18 quinquies ») pour des
        # citations exactes et zéro faux doublon.
        orig_num = art.number
        first_nl = art.body.find("\n")
        first_line = (art.body[:first_nl] if first_nl >= 0 else art.body).strip()
        # (a) écho redondant « Article N » en tête de corps -> on retire la ligne
        if re.fullmatch(rf"Article\s+{re.escape(orig_num)}", first_line, re.I):
            art.body = art.body[first_nl + 1 :].lstrip("\n") if first_nl >= 0 else ""
            first_nl = art.body.find("\n")
            first_line = (art.body[:first_nl] if first_nl >= 0 else art.body).strip()
        lab = (art.label or "").strip()
        if lab and re.fullmatch(r"\d+", lab):  # (b) label numérique
            art.number = f"{orig_num}-{lab}"
            art.label = ""
        elif lab and _LATIN_RE.fullmatch(lab):  # (b') label ordinal latin
            art.number = f"{orig_num} {lab.lower()}"
            art.label = ""
        elif re.match(rf"^(?:{_LATIN})\b\s*[.\-—–)]", first_line, re.I):  # (c) ordinal en corps
            mlat = re.match(rf"^((?:{_LATIN}))\b\s*[.\-—–)]*\s*(.*)$", first_line, re.I | re.S)
            if mlat:
                art.number = f"{orig_num} {mlat.group(1).lower()}"
                rest = art.body[first_nl + 1 :] if first_nl >= 0 else ""
                art.body = (mlat.group(2) + ("\n" + rest if rest else "")).strip()

        if not art.body.strip():
            empty_articles.append(art.number)
            # on garde quand même (article abrogé/renvoi) mais flag
        chunk_texts = chunk_article(art, cfg, ref)
        if len(chunk_texts) > 1:
            multi_chunk += 1
        total = len(chunk_texts)
        hpath = hierarchy_path(art.slots)
        # clé d'unicité incluant la hiérarchie (désambiguïse compilations)
        key = f"{cfg.slug}|{hpath}|{art.number}"
        if key in seen_keys:
            dup_count += 1
        seen_keys.add(key)

        for idx, ctext in enumerate(chunk_texts):
            max_chars = max(max_chars, len(ctext))
            md = {
                "code": meta.get("code", ""),
                "code_label": cfg.label,
                "ref": ref,
                "domain": meta.get("domain", ""),
                "jurisdiction": meta.get("jurisdiction", ""),
                "year": meta.get("year", ""),
                "status": meta.get("status", "in_force"),
                "article_number": art.number,
                "article_label": art.label or None,
                "partie": art.slots[0],
                "livre": art.slots[1],
                "titre": art.slots[2],
                "chapitre": art.slots[3],
                "section": art.slots[4],
                "hierarchy_path": hpath or None,
                "page": art.page,
                "chunk_index": idx,
                "chunk_total": total,
            }
            if note:
                md["note"] = note
            records.append(
                {
                    "expert_slug": EXPERT_SLUG,
                    "source": cfg.source_tag,
                    "content": ctext,
                    "content_sha256": hashlib.sha256(ctext.encode("utf-8")).hexdigest(),
                    "metadata": md,
                }
            )

    # continuité
    nums = [base_int(a.number) for a in articles]
    nums = [n for n in nums if n is not None]
    distinct = sorted(set(nums))
    gaps = [(a, b, b - a - 1) for a, b in zip(distinct, distinct[1:]) if b - a > 1]
    big_gaps = [g for g in gaps if g[2] >= 3]

    stats = {
        "slug": cfg.slug,
        "label": cfg.label,
        "code": meta.get("code", ""),
        "domain": meta.get("domain", ""),
        "jurisdiction": meta.get("jurisdiction", ""),
        "year": meta.get("year", ""),
        "articles": len(articles),
        "chunks": len(records),
        "distinct_numbers": len(distinct),
        "min": distinct[0] if distinct else 0,
        "max": distinct[-1] if distinct else 0,
        "duplicates": dup_count,
        "multi_chunk_articles": multi_chunk,
        "empty_articles": empty_articles,
        "max_chunk_chars": max_chars,
        "big_gaps": big_gaps,
        "note": note,
    }
    return records, stats


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_records: list[dict] = []
    all_stats: list[dict] = []

    for cfg in REGISTRY:
        if not cfg.path.exists():
            print(f"[MANQUANT] {cfg.slug}: {cfg.path}")
            continue
        recs, st = parse_doc(cfg)
        all_records.extend(recs)
        all_stats.append(st)

    # Écriture JSONL
    with OUT_JSONL.open("w", encoding="utf-8") as f:
        for r in all_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Dédup global (sécurité) sur content_sha256
    shas = [r["content_sha256"] for r in all_records]
    uniq_shas = len(set(shas))

    # Rapport
    rep: list[str] = []
    rep.append("# Validation du parsing legal (Phase 2, étape 1)\n")
    rep.append(f"**Documents** : {len(all_stats)} / 14")
    rep.append(f"**Chunks totaux** : {len(all_records)}")
    rep.append(
        f"**Chunks SHA-256 uniques** : {uniq_shas} "
        f"({len(all_records) - uniq_shas} doublons exacts absorbés à l'ingestion)\n"
    )
    rep.append(
        "| Code | Domaine | Jur. | Articles | Chunks | N° distincts | Plage | Doublons n° | Multi-chunk | Max chars |"
    )
    rep.append("|---|---|---|---|---|---|---|---|---|---|")
    tot_art = tot_chunk = 0
    for st in all_stats:
        tot_art += st["articles"]
        tot_chunk += st["chunks"]
        rep.append(
            f"| {st['code']} | {st['domain']} | {st['jurisdiction']} | "
            f"{st['articles']} | {st['chunks']} | {st['distinct_numbers']} | "
            f"{st['min']}-{st['max']} | {st['duplicates']} | "
            f"{st['multi_chunk_articles']} | {st['max_chunk_chars']} |"
        )
    rep.append(f"| **TOTAL** | | | **{tot_art}** | **{tot_chunk}** | | | | | |\n")

    rep.append("## Notes par document\n")
    for st in all_stats:
        rep.append(f"### {st['label']} (`{st['slug']}`)")
        if st["note"]:
            rep.append(f"- NOTE source : {st['note']}")
        if st["duplicates"]:
            rep.append(
                f"- {st['duplicates']} numéros d'article répétés "
                f"(COMPILATION : désambiguïsés par la hiérarchie). OK attendu."
            )
        if st["big_gaps"]:
            g = ", ".join(f"{a}->{b} (-{n})" for a, b, n in st["big_gaps"][:8])
            rep.append(f"- Trous de numérotation (>=3) : {g}")
        if st["empty_articles"]:
            ex = ", ".join(st["empty_articles"][:10])
            rep.append(
                f"- {len(st['empty_articles'])} articles à corps vide "
                f"(abrogés/renvois) : {ex}{'...' if len(st['empty_articles']) > 10 else ''}"
            )
        if st["max_chunk_chars"] > EMBED_TEXT_CAP:
            rep.append(f"- /!\\ max_chunk_chars={st['max_chunk_chars']} > cap {EMBED_TEXT_CAP}")
        rep.append("")

    OUT_REPORT.write_text("\n".join(rep), encoding="utf-8")

    # Résumé console (ASCII safe)
    print("OK parser legal")
    print(f"  documents     : {len(all_stats)}/14")
    print(f"  articles      : {tot_art}")
    print(f"  chunks        : {len(all_records)}")
    print(f"  sha uniques   : {uniq_shas}")
    print(f"  jsonl         : {OUT_JSONL}")
    print(f"  rapport       : {OUT_REPORT}")
    over = [st["slug"] for st in all_stats if st["max_chunk_chars"] > EMBED_TEXT_CAP]
    if over:
        print(f"  /!\\ chunks > cap dans : {over}")


if __name__ == "__main__":
    main()
