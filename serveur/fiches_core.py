"""Cœur du serveur OCR Fiche (rechargé à chaud par serveur_fiches.py, sans relancer Claude Desktop).

Rangement :
    Fiches/
      _base/                         base de connaissance (cachée) : lexique.json, journal_ocr.csv, runs.jsonl
      <Année>/<Matière>/
        <Document>.pdf | .jpg ...    tes scans (on n'y touche pas)
        PDF/<Document>.pdf           le PDF final, fusionné quand toutes les pages sont validées
        _ocr/<Document>/p01.tex      transcription de la page 1 (+ p01.pdf, p01.brut.txt = sortie brute de l'OCR)
        _ocr/_anciens/               anciens fichiers rangés automatiquement
- une fiche = une page de scan : « SVM (3 sur 6) » ;
- RACINE/mes_notations.txt : ton profil d'écriture ; RACINE/_correction-fiches/ : exemples corrigés à la main ;
- stack-local/latex/ : fiche.sty (+ polices/) pour fabriquer les PDF avec XeLaTeX.
À la validation d'une page : les \\illisible sont retirés, les erreurs de l'OCR (brut -> validé) vont dans le
lexique, la page devient un exemple possible (écarté s'il dégrade le taux d'erreur), et le PDF final du document
est fabriqué quand toutes ses pages sont validées.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unicodedata
from pathlib import Path
from typing import Any

RACINE = Path(os.environ.get("FICHES_RACINE") or Path(__file__).resolve().parent / "Fiches").expanduser()
IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff"}
SCANS = IMAGES | {".pdf"}
SORTIE = "_ocr"
CORRECTION = "_correction-fiches"
CALIBRAGE = "_calibrage"          # feuilles de calibrage recopiées à la main (texte connu)
LIBRES = (CORRECTION, CALIBRAGE)   # dossiers d'exemples : rangement libre, toujours « propres »
BASE = "_base"                   # base de connaissance et suivis (dossier caché)
JOURNAL = "journal_ocr.csv"      # taux d'erreur à chaque validation
RUNS = "runs.jsonl"              # suivi des appels à Claude (tokens estimés) écrit par le tableau
LEXIQUE = "lexique.json"         # erreurs de l'OCR apprises des corrections
PDF_FINAL = "PDF"                # dossier visible des PDF fusionnés
ANCIENS = "_anciens"
PAGES_PAR_PARTIE = max(1, int(os.environ.get("FICHES_PAGES_PAR_PARTIE", "1")))   # 1 page de scan = 1 fiche
SEUIL_DECOUPE = max(PAGES_PAR_PARTIE, int(os.environ.get("FICHES_SEUIL_DECOUPE", "1")))   # on découpe dès 2 pages
LATEX = Path(__file__).resolve().parent / "latex"      # fiche.sty et polices/ pour fabriquer les PDF
PAGE = re.compile(r"^(?P<nom>.+?)[ _-]+p(?P<num>\d+)$", re.IGNORECASE)
VERROU_PDF = threading.Lock()   # pdfium n'aime pas les rendus simultanés
COTE_MAX = 1568          # côté le plus long envoyé à l'OCR

# ---------- utilitaires ----------
def slug(texte: str) -> str:
    t = unicodedata.normalize("NFD", texte).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") or "x"


def dans_racine(relatif: str) -> Path:
    """Chemin relatif à RACINE, refusé s'il en sort."""
    p = RACINE
    for part in Path(relatif).parts:
        p = fichier_dans(p, part)
    p = p.resolve()
    if p != RACINE.resolve() and RACINE.resolve() not in p.parents:
        raise ValueError(f"chemin en dehors du dossier des fiches : {relatif}")
    return p


def rel(p: Path) -> str:
    return p.resolve().relative_to(RACINE.resolve()).as_posix()


def sous_dossiers(dossier: Path) -> list[Path]:
    return sorted(
        (d for d in dossier.iterdir() if d.is_dir() and not d.name.startswith(("_", "."))),
        key=lambda d: slug(d.name),
    )


def nb_pages_pdf(fichier: Path) -> int:
    try:
        import pypdfium2 as pdfium
        return len(pdfium.PdfDocument(str(fichier)))
    except Exception:
        return 1


def numero(f: Path) -> int:
    m = PAGE.match(f.stem)
    return int(m["num"]) if m else 0


def statut_tex(contenu: str) -> str:
    return "propre" if re.search(r"^%+\s*statut\s*:\s*propre", contenu, re.M | re.I) else "corr"


def nfc(t: str) -> str:
    return unicodedata.normalize("NFC", t)


def fichier_dans(dossier: Path, nom_fichier: str) -> Path:
    """Le fichier de ce nom dans le dossier, quelle que soit la forme des accents (é composé ou e + accent)."""
    direct = dossier / nom_fichier
    if direct.exists() or not dossier.is_dir():
        return direct
    cible = nfc(nom_fichier)
    for f in dossier.iterdir():
        if nfc(f.name) == cible:
            return f
    return direct


NOM_PAGE = re.compile(r"^(?P<doc>.+?) \((?P<i>\d+) sur (?P<n>\d+)\)$")


def decouper_nom(nom: str) -> tuple[str, int, int]:
    """« SVM (3 sur 6) » -> ("SVM", 3, 6) ; « test » -> ("test", 1, 1)."""
    m = NOM_PAGE.match(nfc(nom))
    return (m["doc"], int(m["i"]), int(m["n"])) if m else (nfc(nom), 1, 1)


def chemin_tex(matiere: Path, nom: str) -> Path:
    doc, i, _ = decouper_nom(nom)
    return fichier_dans(matiere / SORTIE, doc) / f"p{i:02d}.tex"


def _deplacer(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = dest.with_name(dest.stem + "-ancien" + dest.suffix)
    try:
        src.replace(dest)
    except OSError:
        shutil.copy2(src, dest)


def trouver_tex(matiere: Path, nom: str) -> Path | None:
    """Transcription d'une fiche. Les anciens emplacements (_ocr/Nom (i sur n).tex) sont rangés au passage."""
    if matiere.name in LIBRES:                     # exemples corrigés / calibrage : rangement libre
        for d in (matiere / SORTIE, matiere):
            t = fichier_dans(d, f"{nom}.tex")
            if t.is_file():
                return t
        return None
    neuf = chemin_tex(matiere, nom)
    if neuf.is_file():
        return neuf
    for d in (matiere / SORTIE, matiere, matiere / SORTIE / ANCIENS):
        t = fichier_dans(d, f"{nom}.tex")
        if t.is_file():
            for ext in (".tex", ".pdf"):
                if t.with_suffix(ext).is_file():
                    _deplacer(t.with_suffix(ext), neuf.with_suffix(ext))
            return neuf if neuf.is_file() else None
    return None


def titre_tex(contenu: str) -> str | None:
    """Titre d'une fiche : contenu de \\titre{...} (accolades imbriquées), sans les macros de mise en forme."""
    i = contenu.find("\\titre{")
    if i < 0:
        return None
    prof, sortie = 0, []
    for c in contenu[i + len("\\titre"):]:
        if c == "{":
            prof += 1
            if prof == 1:
                continue
        elif c == "}":
            prof -= 1
            if prof == 0:
                break
        sortie.append(c)
    t = re.sub(r"\\couleur\{[^}]*\}", "", "".join(sortie))
    t = re.sub(r"[{}]", "", re.sub(r"\\[A-Za-z]+", "", t))
    return re.sub(r"\s+", " ", t).strip() or None


def fiches(matiere: Path, valide: bool = False) -> list[dict]:
    groupes: dict[str, list[Path]] = {}
    for f in sorted(matiere.iterdir(), key=lambda p: (p.name.lower())):
        if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in SCANS:
            m = PAGE.match(f.stem)
            groupes.setdefault(nfc((m["nom"] if m else f.stem).strip()), []).append(f)
    resultat, vus = [], set()
    for nom, fichiers in groupes.items():
        fichiers.sort(key=numero)
        pages = []
        for f in fichiers:
            n = nb_pages_pdf(f) if f.suffix.lower() == ".pdf" else 1
            pages += [{"fichier": rel(f), "page": i} for i in range(n)]
        # un scan de plusieurs pages devient une fiche par page : « SVM (1 sur 6) »...
        # (sauf les exemples corrigés de _correction-fiches, gardés entiers)
        parties = [pages]
        if len(pages) > SEUIL_DECOUPE and not valide:
            n = -(-len(pages) // PAGES_PAR_PARTIE)
            taille = -(-len(pages) // n)
            parties = [pages[i:i + taille] for i in range(0, len(pages), taille)]
        for i, pp in enumerate(parties, 1):
            nom_p = nom if len(parties) == 1 else f"{nom} ({i} sur {len(parties)})"
            titre, statut = nom_p.replace("_", " "), "scan"
            tex = trouver_tex(matiere, nom_p)
            if tex:
                contenu = tex.read_text(encoding="utf-8", errors="ignore")
                titre = titre_tex(contenu) or titre
                if len(parties) > 1 and not titre.endswith(f"({i}/{len(parties)})"):
                    titre += f" ({i}/{len(parties)})"
                statut = "propre" if valide else statut_tex(contenu)
            ident = slug(nom_p)
            while ident in vus:
                ident += "-b"
            vus.add(ident)
            resultat.append({"id": ident, "nom": nom_p, "titre": titre, "statut": statut,
                             "dossier": rel(matiere), "pages": pp, "pdf": infos_pdf(tex),
                             "doc": nom, "page_doc": i, "n_doc": len(parties)})
    return resultat



# ---------- PDF ----------
def trouver_xelatex() -> str | None:
    voulu = os.environ.get("XELATEX")
    if voulu and Path(voulu).is_file():
        return voulu
    if shutil.which("xelatex"):
        return shutil.which("xelatex")
    bases = [os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA"), str(Path.home())]
    candidats = []
    for b in filter(None, bases):
        candidats += [Path(b) / "TinyTeX" / "bin" / "windows" / "xelatex.exe",
                      Path(b) / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64" / "xelatex.exe"]
    for racine in (Path("C:/Program Files/MiKTeX/miktex/bin/x64"), Path("C:/texlive")):
        if racine.is_dir():
            candidats += [racine / "xelatex.exe"] + sorted(racine.glob("*/bin/windows/xelatex.exe"), reverse=True)
    return next((str(c) for c in candidats if c.is_file()), None)


def erreurs_log(log: str) -> str:
    lignes, sortie = log.splitlines(), []
    for i, l in enumerate(lignes):
        if l.startswith("!") or "Fatal error" in l or "not found" in l:
            sortie += lignes[i:i + 3]
    return "\n".join(sortie[:12]) or "\n".join(lignes[-8:])


FERME = {"{": "}", "$": "$", "$$": "$$", "\\[": "\\]", "\\(": "\\)"}
JETON = re.compile(r"\\\\|\\[{}$%&#_]|\\(begin|end)\s*\{([^{}]*)\}|\\\[|\\\]|\\\(|\\\)|\$\$|[{}$]")


def _fermant(x: str) -> str:
    return "\\end{" + x[4:] + "}" if x.startswith("env:") else FERME[x]


def equilibrer(corps: str) -> str:
    """Répare la structure : accolades, $, \\[ \\], \\begin/\\end. Ajoute les fermetures manquantes,
    retire les fermetures orphelines. Les commentaires (%) sont ignorés."""
    pile: list[str] = []
    sortie: list[str] = []

    def fermer_depuis(k: int) -> str:
        x = "".join(_fermant(e) for e in reversed(pile[k:])); del pile[k:]; return x

    for ligne in corps.split("\n"):
        if not ligne.strip():           # fin de paragraphe : une accolade ou des maths ne peuvent pas la traverser
            k = next((i for i, e in enumerate(pile) if not e.startswith("env:")), None)
            if k is not None:
                ajout = fermer_depuis(k)
                d = max((i for i, l in enumerate(sortie) if l.strip()), default=None)
                if d is None: sortie.append(ajout)
                else: sortie[d] += ajout
            sortie.append(ligne); continue
        m = re.search(r"(?<!\\)%", ligne)
        code, com = (ligne[:m.start()], ligne[m.start():]) if m else (ligne, "")
        res, i = [], 0
        for j in JETON.finditer(code):
            res.append(code[i:j.start()]); i = j.end(); t = j.group(0)
            if j.group(1) == "begin":
                pile.append("env:" + j.group(2).strip()); res.append(t); continue
            if j.group(1) == "end":
                cible = "env:" + j.group(2).strip()
            elif t in ("}", "\\]", "\\)"):
                cible = {"}": "{", "\\]": "\\[", "\\)": "\\("}[t]
            elif t in ("$", "$$"):          # $ ferme seulement s'il est en haut de la pile ($ dans \text{...} = nouvelles maths)
                if not pile or pile[-1] != t:
                    pile.append(t); res.append(t); continue
                cible = t
            elif t in ("{", "\\[", "\\("):
                pile.append(t); res.append(t); continue
            else:                       # \\, \{, \}, \$, \%...
                res.append(t); continue
            if cible in pile:           # on ferme d'abord ce qui est resté ouvert à l'intérieur
                while pile[-1] != cible:
                    res.append(_fermant(pile.pop()))
                pile.pop(); res.append(t)
            # sinon : fermeture orpheline, on la retire
        res.append(code[i:])
        k = next((i for i, e in enumerate(pile) if e in ("$", "\\(")), None)   # maths en ligne : fermées en fin de ligne
        if k is not None:
            res.append(fermer_depuis(k))
        sortie.append("".join(res) + com)
    if pile:
        sortie.append("".join(_fermant(x) for x in reversed(pile)))
    return "\n".join(sortie)


def _premiere_erreur(log: str) -> tuple[str, int | None]:
    lignes = log.splitlines()
    for i, l in enumerate(lignes):
        if l.startswith("!"):
            for k in lignes[i:i + 25]:
                m = re.match(r"l\.(\d+)", k)
                if m:
                    return l[1:].strip(), int(m.group(1))
            return l[1:].strip(), None
    return erreurs_log(log), None


def _xelatex(contenu: str, dest: Path | None = None, extra: str | None = None) -> dict[str, Any]:
    xl = trouver_xelatex()
    if not xl:
        return {"ok": False, "fatal": True, "message": "XeLaTeX introuvable sur ce PC (installe TinyTeX ou MiKTeX, ou définis XELATEX dans la configuration)."}
    if not (LATEX / "fiche.sty").is_file():
        return {"ok": False, "fatal": True, "message": f"fiche.sty manquant : place-le dans {LATEX}"}
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "fiche.tex"
        src.write_text(contenu, encoding="utf-8")
        env = dict(os.environ, TEXINPUTS=str(LATEX) + os.pathsep + (str(extra) + os.pathsep if extra else ""))
        cmd = [xl, "-interaction=nonstopmode", "-halt-on-error", "-output-directory", tmp, "-jobname", "fiche", str(src)]
        try:
            r = subprocess.run(cmd, cwd=LATEX, capture_output=True, timeout=240, env=env,
                               creationflags=0x08000000 if os.name == "nt" else 0)
        except subprocess.TimeoutExpired:
            return {"ok": False, "fatal": True, "message": "La compilation a pris plus de 4 minutes (première utilisation : XeLaTeX installe peut-être des paquets)."}
        pdf, log = Path(tmp) / "fiche.pdf", Path(tmp) / "fiche.log"
        if r.returncode == 0 and pdf.is_file():
            if dest:
                shutil.copyfile(pdf, dest)
            return {"ok": True, "message": "", "log": log.read_text(encoding="utf-8", errors="ignore") if log.is_file() else ""}
        texte = log.read_text(encoding="utf-8", errors="ignore") if log.is_file() else r.stdout.decode("utf-8", "ignore")
        msg, ligne = _premiere_erreur(texte)
        return {"ok": False, "message": msg, "ligne": ligne, "detail": erreurs_log(texte)}


def preparer(corps: str) -> str:
    if os.environ.get("FICHES_SAUT_PAGE") != "1":      # le texte s'écoule d'une page de scan à l'autre
        corps = re.sub(r"^[ \t]*\\(clearpage|newpage)[ \t]*$", "", corps, flags=re.M)
    corps = re.sub(r"^[ \t]*(…|\.\.\.)[ \t]*$", "", corps, flags=re.M)     # marque « en cours de lecture »
    corps = re.sub(r"\\(?:sub)*section\*?\{", r"\\titre{", corps)          # fiche.sty n'a pas \section : c'est un titre
    corps = restaurer_lignes(corps)
    corps = re.sub(r"\\mathbb\{1\}", r"\\mathbf{1}", corps)                 # indicatrice : amssymb n'a pas de 1 ajouré
    return maths_propres(equilibrer(corps))



# ---------- Dessins : la zone indiquée par l'OCR est découpée dans le scan et mise dans le PDF ----------
DESSIN = re.compile(r"\\dessin\s*\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\]\s*\{")


def _image_page(chemin: Path, page: int, cache: dict):
    from PIL import Image as PIL, ImageOps
    cle = (str(chemin), page)
    if cle not in cache:
        if chemin.suffix.lower() == ".pdf":
            import pypdfium2 as pdfium
            with VERROU_PDF:
                doc = pdfium.PdfDocument(str(chemin))
                try:
                    pg = doc[max(0, min(page, len(doc) - 1))]
                    w, h = pg.get_size()
                    cache[cle] = pg.render(scale=2600 / max(w, h)).to_pil().convert("RGB")
                finally:
                    doc.close()
        else:
            if chemin.suffix.lower() == ".heic":
                from pillow_heif import register_heif_opener
                register_heif_opener()
            cache[cle] = ImageOps.exif_transpose(PIL.open(chemin)).convert("RGB")
    return cache[cle]


def _nettoyer_dessin(im):
    """Fond du papier rendu blanc (balance des blancs sur la couleur du papier), encres gardées."""
    from PIL import ImageStat
    fond = [max(60, v) for v in ImageStat.Stat(im).median]          # le papier occupe la majorité de la zone
    im = im.point([min(255, int(i * 255 / fond[c])) for c in range(3) for i in range(256)])
    masque = im.convert("L").point(lambda v: 255 if v > 228 else 0)
    im.paste((255, 255, 255), mask=masque)
    return im


def source_page(matiere: Path | None, marque: str | None, tete: str, nom: str | None, page: int | None = None) -> tuple[Path, int] | None:
    """Scan et numéro de page (0 = première) d'une page de fiche, d'après « %% page: SVM.pdf (p. 3) »,
    sinon \\source{...} et le nom « SVM (3 sur 6) »."""
    if matiere is None:
        return None
    if marque:
        m = re.match(r"\s*(.*?)(?:\s*\(p\.\s*(\d+)\))?\s*$", marque)
        f = fichier_dans(matiere, m[1].strip())
        if f.is_file():
            return f, (int(m[2]) - 1 if m[2] else 0)
    m = re.search(r"\\source\{([^}]*)\}", tete)
    if m:
        f = fichier_dans(matiere, m[1].strip())
        if f.is_file():
            if page is not None:
                return f, page
            n = re.search(r"\((\d+) sur \d+\)\s*$", nom or "")
            return f, (int(n[1]) - 1 if n else 0)
    return None


def _accolade_fin(t: str, i: int) -> int:
    """t[i] suit une accolade ouvrante : indice juste après l'accolade fermante correspondante."""
    prof = 1
    while i < len(t) and prof:
        if t[i] == "\\":
            i += 2; continue
        prof += (t[i] == "{") - (t[i] == "}")
        i += 1
    return i


class Decoupe:
    """Découpe les zones de dessin dans les scans pour une compilation."""
    def __init__(self, matiere, tete, nom, page, dossier_img: Path):
        self.matiere, self.tete, self.nom, self.page, self.dir = matiere, tete, nom, page, dossier_img
        self.cache: dict = {}
        self.k = 0

    def __call__(self, marque, coords) -> tuple[str, float, float] | None:
        x1, y1, x2, y2 = coords
        src = source_page(self.matiere, marque, self.tete, self.nom, self.page)
        if not src or x2 <= x1 or y2 <= y1:
            return None
        try:
            im = _image_page(src[0], src[1], self.cache)
            W, H = im.size
            pad = 1.5
            b = (max(0, (x1 - pad) / 100 * W), max(0, (y1 - pad) / 100 * H), min(W, (x2 + pad) / 100 * W), min(H, (y2 + pad) / 100 * H))
            if b[2] - b[0] <= 20 or b[3] - b[1] <= 20:
                return None
            self.k += 1
            fic = f"dessin{self.k}.png"
            _nettoyer_dessin(im.crop(tuple(int(v) for v in b))).save(self.dir / fic)
            return fic, b[0] / W, (b[2] - b[0]) / W
        except Exception:
            return None


ACOTE = re.compile(r"\\begin\{acote\}\s*(?:\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\])?\s*\{")


def placer_dessins(corps: str, tete: str, matiere: Path | None, nom: str | None, dossier_img: Path, page: int | None = None) -> str:
    """\\dessin[x1,y1,x2,y2]{...} -> l'image découpée dans le scan, même taille et même place horizontale ;
    \\begin{acote}[x1,y1,x2,y2]{...} texte \\end{acote} -> dessin et texte côte à côte, comme sur la feuille.
    Le nombre de lignes ne change pas (les numéros de ligne des erreurs restent justes)."""
    dec = Decoupe(matiere, tete, nom, page, dossier_img)

    def marque_avant(pos: int) -> str | None:
        ms = list(re.finditer(r"^%%\s*page:\s*(.*)$", corps[:pos], re.M))
        return ms[-1][1] if ms else None

    def remplacer(texte: str, rx, fabrique) -> str:
        sortie, i = [], 0
        for m in rx.finditer(texte):
            if m.start() < i:
                continue
            fin = _accolade_fin(texte, m.end())
            desc = texte[m.end():fin - 1]
            ligne_deb = texte.rfind("\n", 0, m.start()) + 1
            if texte[ligne_deb:m.start()].lstrip().startswith("%"):
                continue
            coords = tuple(float(v) for v in m.groups()) if m.group(1) else None
            sortie.append(texte[i:m.start()])
            sortie.append(fabrique(coords, desc, m.start()) + "\n" * texte[m.start():fin].count("\n"))
            i = fin
        return "".join(sortie) + texte[i:]

    def un_dessin(coords, desc, pos):
        r = dec(marque_avant(pos), coords) if coords else None
        if not r:
            return "\\dessin{" + desc.replace("\n", " ") + "}"
        return f"\\dessinimg{{{r[1]:.3f}}}{{{r[2]:.3f}}}{{{r[0]}}}"

    def a_cote(coords, desc, pos):
        r = dec(marque_avant(pos), coords) if coords else None
        if not r:
            return "\\begin{acote}{" + desc.replace("\n", " ") + "}"
        cote = "g" if (coords[0] + coords[2]) / 2 < 50 else "d"
        return f"\\begin{{acoteimg}}{{{cote}}}{{{r[2]:.3f}}}{{{r[0]}}}"

    corps = remplacer(corps, ACOTE, a_cote)
    # chaque \end{acote} qui suit un acoteimg devient \end{acoteimg}
    sortie, ouvert = [], []
    for m in re.finditer(r"\\begin\{acote(img)?\}|\\end\{acote\}|[^\\]+|\\", corps):
        t = m[0]
        if t.startswith("\\begin{acote"):
            ouvert.append(bool(m[1]))
        elif t == "\\end{acote}" and ouvert:
            t = "\\end{acoteimg}" if ouvert.pop() else t
        sortie.append(t)
    corps = "".join(sortie)
    return remplacer(corps, DESSIN, un_dessin)


# ---------- Lignes trop longues : une formule qui dépasse est coupée en plusieurs lignes ----------
REL = re.compile(r"\\(?:Rightarrow|Leftrightarrow|iff|implies|leq|geq|le|ge|neq|approx|sim)(?![A-Za-z])|=|<|>")


def _points_coupure(m: str) -> list[tuple[int, int]]:
    """Positions (index, priorité) où couper une formule, au niveau 0 des accolades et parenthèses."""
    pts, prof, i, n = [], 0, 0, len(m)
    while i < n:
        c = m[i]
        if m.startswith("\\left", i) and not m[i + 5:i + 6].isalpha():
            prof += 1; i += 5; continue
        if m.startswith("\\right", i) and not m[i + 6:i + 7].isalpha():
            prof = max(0, prof - 1); i += 6; continue
        if c == "\\":
            mt = re.match(r"\\text\s*\{\s*,?\s*(avec|où|et|donc|soit|si|car|alors|sinon)\b", m[i:])
            if mt and prof == 0:
                pts.append((i, 0))
            mr = REL.match(m, i)
            if mr and prof == 0:
                pts.append((i, 1))
            j = i + 1
            while j < n and m[j].isalpha():
                j += 1
            i = max(j, i + 2); continue
        if c in "{([":
            prof += 1
        elif c in "})]":
            prof = max(0, prof - 1)
        elif prof == 0:
            if REL.match(m, i):
                pts.append((i, 1))
            elif c in "+-" and i > 0:
                avant = m[:i].rstrip()
                if avant and avant[-1] not in "=<>+-({[^_,&" and not avant.endswith(("\\\\", "\\cdot")):
                    pts.append((i, 2))
            elif c == "," and m[i + 1:i + 2] in (" ", "\\"):
                pts.append((i + 1, 3))
        i += 1
    return pts


def _couper_formule(m: str) -> str | None:
    pts = [p for p in _points_coupure(m) if 0 < p[0] < len(m) - 1 and m[:p[0]].strip()]
    if not pts:
        return None
    L = len(m)
    dans = [p for p in pts if .25 * L <= p[0] <= .8 * L] or pts
    i = min(dans, key=lambda p: (p[1], abs(p[0] - L / 2)))[0]
    return m[:i].rstrip() + " \\\\ " + m[i:].lstrip()


def _bloc_maths(texte: str, pos: int) -> tuple[int, int, str, str] | None:
    """Bloc de maths en display qui contient la position pos : (début, fin, type, contenu)."""
    for ouv, fer, typ in (("\\[", "\\]", "d"), ("\\begin{multline*}", "\\end{multline*}", "m")):
        a = texte.rfind(ouv, 0, pos + 1)
        while a >= 0 and texte[a - 1:a] == "\\" and typ == "d":
            a = texte.rfind(ouv, 0, a)
        if a < 0:
            continue
        b = texte.find(fer, a + len(ouv))
        if b >= 0 and b + len(fer) >= pos - 200:
            return a, b + len(fer), typ, texte[a + len(ouv):b]
    return None


def _arg(t: str, i: int) -> int:
    """Indice juste après l'accolade fermante de l'argument qui commence à t[i] == '{'."""
    prof = 0
    while i < len(t):
        c = t[i]
        if c == "\\":
            i += 2; continue
        prof += (c == "{") - (c == "}")
        i += 1
        if prof == 0:
            return i
    return len(t)


def _souligne_en_maths(m: str) -> str:
    """\\souligne (ulem) ne marche pas en mode maths : \\underline à la place."""
    sortie, i = [], 0
    while True:
        j = m.find("\\souligne{", i)
        if j < 0:
            return "".join(sortie) + m[i:]
        k = _arg(m, j + 9)
        a = m[j + 10:k - 1].strip()
        sortie.append(m[i:j])
        if a.startswith("$") and a.endswith("$") and a.count("$") == 2:
            sortie.append("\\underline{" + a[1:-1] + "}")
        else:
            sortie.append("\\underline{\\text{" + a + "}}")
        i = k


DISPLAY = re.compile(r"\\\[(.*?)\\\]|\\begin\{(multline\*|align\*|equation\*|gather\*|aligned)\}(.*?)\\end\{\2\}", re.S)


def maths_propres(corps: str) -> str:
    return DISPLAY.sub(lambda m: _souligne_en_maths(m[0]) if "\\souligne{" in m[0] else m[0], corps)


def restaurer_lignes(corps: str) -> str:
    """Les lignes mises de côté (« À VÉRIFIER ») sont retentées : le serveur sait peut-être les compiler maintenant."""
    return re.sub(r"^\\mbox\{\\couleur\{rouge\}\{\[ligne à vérifier\]\}\} % À VÉRIFIER \([^)]*\) : (.*)$", r"\1", corps, flags=re.M)


def _cellule_en_texte(cel: str) -> str:
    """Cellule de tableau en maths (avec des \\text{...}) -> texte qui peut aller à la ligne."""
    morceaux, i = [], 0
    while True:
        j = cel.find("\\text{", i)
        if j < 0:
            break
        k = _arg(cel, j + 5)
        morceaux += [("m", cel[i:j]), ("t", cel[j + 6:k - 1])]
        i = k
    morceaux.append(("m", cel[i:]))
    return "".join(x if t == "t" else (f" ${x.strip()}$ " if x.strip() else x) for t, x in morceaux).strip()


ACCOLADE = re.compile(r"\\left\\\{\s*\\begin\{array\}\{[^}]*\}(.*?)\\end\{array\}\s*\\right\s*\.", re.S)


def accolade_en_texte(m: str) -> str | None:
    """« { ligne 1 / ligne 2 » écrit avec array + \\text : le texte ne peut pas aller à la ligne.
    On le remet dans une boîte de la largeur de la page, où il passe à la ligne tout seul."""
    def conv(a):
        if "\\text{" not in a[1]:
            return a[0]
        rangs = [r for r in re.split(r"\\\\(?:\[[^\]]*\])?", a[1]) if r.strip()]
        return ("\\left\\{\\parbox{\\dimexpr\\linewidth-2.5em\\relax}{\\raggedright "
                + " \\\\ ".join(_cellule_en_texte(r) for r in rangs) + "}\\right.")
    neuf = ACCOLADE.sub(conv, m)
    return neuf if neuf != m else None


def _ligne_en_maths(r: str) -> str:
    """« texte $Y$ suite » -> « \text{texte } Y \text{ suite} » (inverse de _cellule_en_texte)."""
    morceaux = re.split(r"(?<!\\)\$", r)
    out = []
    for k, m in enumerate(morceaux):
        if k % 2:
            out.append(m)
        elif m.strip():
            out.append("\\text{" + m + "}")
    return " ".join(out).strip()


def defaire_mise_en_page(corps: str) -> str:
    """Les ajustements faits pour le PDF (texte qui passe à la ligne, formule réduite) restent hors du .tex :
    on remet la forme d'origine, lisible dans l'aperçu."""
    def acc(m):
        rangs = [x for x in re.split(r"\s\\\\\s", m[1]) if x.strip()]
        return "\\left\\{ \\begin{array}{l} " + " \\\\ ".join(_ligne_en_maths(x.strip()) for x in rangs) + " \\end{array} \\right."
    sortie, i = [], 0
    debut = "\\left\\{\\parbox{\\dimexpr\\linewidth-2.5em\\relax}{\\raggedright "
    while True:
        j = corps.find(debut, i)
        if j < 0:
            break
        fin = _accolade_fin(corps, j + len(debut))
        interieur = corps[j + len(debut):fin - 1]
        k = fin
        if corps.startswith("\\right.", k):
            k += len("\\right.")
        sortie += [corps[i:j], acc([None, interieur])]
        i = k
    corps = "".join(sortie) + corps[i:]
    return re.sub(r"\\ajuste\{\$\\displaystyle (.*?)\$\}", r"\1", corps)


def reparer_debordement(corps: str, n1: int, n2: int) -> str | None:
    """Ligne(s) n1..n2 du corps trop larges : coupe la formule en display, ou passe en display une longue formule en ligne."""
    lignes = corps.split("\n")
    if not 1 <= n1 <= len(lignes):
        return None
    n2 = max(n1, min(n2, len(lignes)))
    debut = sum(len(l) + 1 for l in lignes[:n1 - 1])
    fin = debut + sum(len(l) + 1 for l in lignes[n1 - 1:n2])
    bloc = _bloc_maths(corps, fin - 1)
    if bloc and bloc[1] >= debut:
        a, b, typ, m = bloc
        acc = accolade_en_texte(m)
        if acc:
            ancien = corps[a:b]
            neuf = (corps[a:b].replace(m, acc)).replace("\n", " ") + "\n" * ancien.count("\n")
            return corps[:a] + neuf + corps[b:]
        if typ == "d":
            c = _couper_formule(m.strip())
            neuf = "\\begin{multline*} " + c + " \\end{multline*}" if c else "\\[ \\ajuste{$\\displaystyle " + m.strip() + "$} \\]"
        else:
            rangs = re.split(r"(?<!\\)\\\\(?!\[)", m)
            k = max(range(len(rangs)), key=lambda r: len(rangs[r]))
            c = _couper_formule(rangs[k].strip())
            if not c:
                return None
            rangs[k] = " " + c + " "
            neuf = "\\begin{multline*}" + "\\\\".join(rangs) + "\\end{multline*}"
        ancien = corps[a:b]
        neuf = neuf.replace("\n", " ") + "\n" * ancien.count("\n")
        return corps[:a] + neuf + corps[b:] if neuf != ancien else None
    # paragraphe : la plus longue formule en ligne passe en display
    zone = corps[debut:fin]
    for mm in re.finditer(r"(?<!\\)\$([^$]+)(?<!\\)\$", zone):
        acc = accolade_en_texte(mm[1])
        if acc:
            return corps[:debut] + zone[:mm.start()] + "$" + acc.replace("\n", " ") + "$" + "\n" * mm[0].count("\n") + zone[mm.end():] + corps[fin:]
    cands = [mm for mm in re.finditer(r"(?<!\\)\$([^$]{25,})(?<!\\)\$", zone)]
    if not cands:
        return None
    mm = max(cands, key=lambda x: len(x[1]))
    return corps[:debut] + zone[:mm.start()] + "\\[ " + mm[1] + " \\]" + zone[mm.end():] + corps[fin:]


def debordements(log: str, decal: int) -> list[tuple[float, int, int]]:
    sortie = []
    for m in re.finditer(r"Overfull \\hbox \(([\d.]+)pt too wide\) (?:detected at line (\d+)|in paragraph at lines (\d+)--(\d+))", log):
        larg = float(m[1])
        if larg < 6:
            continue
        a = int(m[2] or m[3]) - decal
        b = int(m[2] or m[4]) - decal
        sortie.append((larg, a, b))
    return sorted(sortie, reverse=True)


def compiler_corps(tete: str, corps: str, dest: Path | None = None, essais: int = 25,
                   matiere: Path | None = None, nom: str | None = None, page: int | None = None) -> dict[str, Any]:
    """Compile ; à chaque erreur dans le corps, la ligne fautive est mise en commentaire (repère « À VÉRIFIER »)
    et remplacée par une marque visible, puis on recompile. Le PDF sort donc toujours.
    Les \\dessin[x1,y1,x2,y2]{...} deviennent l'image découpée dans le scan (le .tex garde les coordonnées)."""
    with tempfile.TemporaryDirectory() as imgs:
        return _compiler_corps(tete, corps, dest, essais, matiere, nom, Path(imgs), page)


def _compiler_corps(tete, corps, dest, essais, matiere, nom, imgs: Path, page=None) -> dict[str, Any]:
    corps = preparer(corps)
    avant = tete + "\n\n\\begin{document}\n"
    decal = avant.count("\n")
    erreurs: list[dict[str, Any]] = []
    deja_vus: set = set()
    dernier_fix = None
    for _ in range(essais):
        res = _xelatex(avant + placer_dessins(corps, tete, matiere, nom, imgs, page) + "\n\\end{document}\n", dest, str(imgs))
        if res.get("fatal"):
            return {**res, "corps": corps, "erreurs": erreurs}
        if not res["ok"] and dernier_fix is not None:      # la coupure a cassé la compilation : on revient en arrière
            corps, dernier_fix = dernier_fix, None
            continue
        dernier_fix = None
        if res["ok"]:
            # le PDF compile : on coupe les formules qui dépassent de la page, puis on recompile
            for larg, a, b in debordements(res.get("log", ""), decal):
                if (a, b) in deja_vus:
                    continue
                deja_vus.add((a, b))
                neuf = reparer_debordement(corps, a, b)
                if neuf and neuf != corps:
                    dernier_fix, corps = corps, neuf
                    break
            else:
                return {**res, "corps": corps, "erreurs": erreurs}
            continue
        lignes = corps.split("\n")
        n = (res.get("ligne") or 0) - decal          # numéro de ligne dans le corps (1 = première)
        if not 1 <= n <= len(lignes) or "À VÉRIFIER" in lignes[n - 1]:
            # erreur hors du corps (ou déjà traitée) : on neutralise la dernière ligne non vide pas encore traitée
            cand = [k for k, l in enumerate(lignes, 1) if l.strip() and "À VÉRIFIER" not in l and not l.lstrip().startswith("%")]
            if not cand:
                return {**res, "corps": corps, "erreurs": erreurs}
            n = cand[-1] if not 1 <= n <= len(lignes) else max([k for k in cand if k < n] or [cand[0]])
        orig = lignes[n - 1]
        erreurs.append({"ligne": n, "message": res["message"], "extrait": orig.strip()[:200]})
        lignes[n - 1] = "\\mbox{\\couleur{rouge}{[ligne à vérifier]}} % À VÉRIFIER (" + res["message"][:80].replace("%", "") + ") : " + orig.strip()
        corps = equilibrer("\n".join(lignes))
    return {"ok": False, "message": "trop d'erreurs LaTeX dans cette transcription", "corps": corps, "erreurs": erreurs}


def matiere_de(tex: Path) -> tuple[Path, int | None]:
    """Dossier de la matière d'un .tex, et numéro de page dans le scan (0 = première) si on le connaît."""
    m = re.match(r"p(\d+)$", tex.stem)
    for d in tex.parents:
        if d.name == SORTIE:
            return d.parent, (int(m[1]) - 1 if m else None)
    return tex.parent, None


def compiler(tex: Path) -> dict[str, Any]:
    texte = tex.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"\\begin\{document\}\n?(.*?)\n?\\end\{document\}", texte, re.S)
    if not m:
        return {"ok": False, "message": "le fichier .tex n'a pas de \\begin{document} ... \\end{document}"}
    tete, corps = texte[:m.start()].rstrip("\n"), m.group(1)
    matiere, page = matiere_de(tex)
    res = compiler_corps(tete, corps, tex.with_suffix(".pdf"), matiere=matiere, nom=tex.stem, page=page)
    res["corps"] = defaire_mise_en_page(res["corps"])
    if res["ok"] and res["corps"].strip() != corps.strip():      # on garde dans le .tex ce qui a été réparé
        tex.write_text(tete + "\n\n\\begin{document}\n" + res["corps"].strip() + "\n\\end{document}\n", encoding="utf-8")
        os.utime(tex.with_suffix(".pdf"))
    msg = res["message"]
    if res["ok"] and res["erreurs"]:
        msg = f"{len(res['erreurs'])} ligne(s) mise(s) de côté, repère « À VÉRIFIER » dans le LaTeX : " + \
              ", ".join(f"l. {e['ligne']} ({e['message'][:50]})" for e in res["erreurs"][:5])
    return {"ok": res["ok"], "message": msg if not res["ok"] else msg, "corps": res["corps"], "erreurs": res["erreurs"],
            "repare": res["ok"] and res["corps"].strip() != corps.strip()}


def infos_pdf(tex: Path | None) -> dict[str, Any] | None:
    if not tex:
        return None
    pdf = tex.with_suffix(".pdf")
    if not pdf.is_file():
        return None
    return {"fichier": rel(pdf), "pages": nb_pages_pdf(pdf), "a_jour": pdf.stat().st_mtime >= tex.stat().st_mtime - 1}


def lire_runs(n: int = 1500) -> list[dict[str, Any]]:
    f = RACINE / BASE / RUNS
    if not f.is_file():
        return []
    sortie = []
    for l in f.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:]:
        try:
            o = json.loads(l)
            if isinstance(o, dict):
                sortie.append(o)
        except ValueError:
            pass
    return sortie


def ecrire_runs(lignes: str) -> int:
    bonnes = []
    for l in lignes.splitlines()[:500]:
        try:
            o = json.loads(l)
        except ValueError:
            continue
        if isinstance(o, dict) and len(l) < 2000:
            bonnes.append(json.dumps(o, ensure_ascii=False))
    if bonnes:
        (RACINE / BASE).mkdir(parents=True, exist_ok=True)
        with open(RACINE / BASE / RUNS, "a", encoding="utf-8") as j:
            j.write("\n".join(bonnes) + "\n")
    return len(bonnes)


# ---------- Rangement automatique ----------
def ranger(racine: Path) -> None:
    """Range les anciens fichiers : suivis à la racine -> _base/, fichiers isolés de _ocr/ -> _ocr/_anciens/."""
    base = racine / BASE
    for ancien, neuf in (("_journal_ocr.csv", JOURNAL), ("_runs_ocr.jsonl", RUNS)):
        a = racine / ancien
        if a.is_file():
            base.mkdir(exist_ok=True)
            with open(base / neuf, "a", encoding="utf-8") as f:
                f.write(a.read_text(encoding="utf-8", errors="ignore"))
            a.unlink()
    vieux = racine / SORTIE                       # _ocr/ à la racine : restes d'une ancienne version
    if vieux.is_dir():
        for f in list(vieux.iterdir()):
            _deplacer(f, base / ANCIENS / f.name)
        try:
            vieux.rmdir()
        except OSError:
            pass


def ranger_matiere(matiere: Path) -> None:
    """Après fiches() (qui a déplacé les transcriptions reconnues) : ce qui reste en vrac dans _ocr/ part dans _ocr/_anciens/."""
    s = matiere / SORTIE
    if not s.is_dir():
        return
    for f in list(s.iterdir()):
        if f.is_file() and f.suffix.lower() in (".tex", ".pdf", ".log", ".aux"):
            _deplacer(f, s / ANCIENS / f.name)


# ---------- Base de connaissance ----------
def corps_de(contenu: str) -> str:
    m = re.search(r"\\begin\{document\}\n?(.*?)\\end\{document\}", contenu, re.S)
    return (m[1] if m else contenu).strip()


def nettoyer_valide(corps: str) -> str:
    """Page validée : les \\illisible{...} ont été relus, on garde le mot ; les vides disparaissent."""
    sortie, i = [], 0
    while True:
        j = corps.find("\\illisible{", i)
        if j < 0:
            break
        fin = _accolade_fin(corps, j + 11)
        sortie += [corps[i:j], corps[j + 11:fin - 1].strip()]
        i = fin
    return "".join(sortie) + corps[i:]


JETON_TEX = re.compile(r"\\[A-Za-z]+\*?|\\.|[A-Za-zÀ-ÖØ-öø-ÿ0-9]+|[^\sA-Za-zÀ-ÖØ-öø-ÿ0-9]")


def _jetons(t: str) -> list[str]:
    t = re.sub(r"^%%.*$", "", t, flags=re.M)
    return JETON_TEX.findall(t)


def erreurs_ocr(brut: str, juste: str) -> list[tuple[str, str]]:
    """Paires (lu par l'OCR, version validée) : petits remplacements seulement (≤ 4 jetons de chaque côté)."""
    import difflib
    a, b = _jetons(brut), _jetons(juste)
    if not a or not b or len(a) > 20000 or len(b) > 20000:
        return []
    paires = []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace" or i2 - i1 > 4 or j2 - j1 > 4:
            continue
        x, y = " ".join(a[i1:i2]), " ".join(b[j1:j2])
        if x.replace(" ", "") == y.replace(" ", "") or not y.strip():
            continue
        if all(len(t) == 1 and not t.isalnum() for t in a[i1:i2] + b[j1:j2]):   # ponctuation seule
            continue
        # un jeton de contexte de chaque côté : « \sigma ^ 2 » n'est pas « \lambda »
        g = a[i1 - 1] if i1 > 0 else ""
        d = a[i2] if i2 < len(a) else ""
        paires.append((f"{g} {x} {d}".strip(), f"{g} {y} {d}".strip()))
    return paires


def lire_lexique() -> dict[str, Any]:
    f = RACINE / BASE / LEXIQUE
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ajouter_lexique(paires: list[tuple[str, str]], matiere: str) -> None:
    if not paires:
        return
    from datetime import date
    lex = lire_lexique()
    for x, y in paires:
        cle = x + " => " + y
        e = lex.setdefault(cle, {"lu": x, "juste": y, "n": 0, "matieres": []})
        e["n"] += 1
        e["vu"] = date.today().isoformat()
        if matiere not in e["matieres"]:
            e["matieres"].append(matiere)
    (RACINE / BASE).mkdir(parents=True, exist_ok=True)
    (RACINE / BASE / LEXIQUE).write_text(json.dumps(lex, ensure_ascii=False, indent=1), encoding="utf-8")


def lexique_utile(n: int = 40) -> list[dict[str, Any]]:
    """Les erreurs à rappeler à l'OCR : les plus fréquentes ; si une même lecture a plusieurs corrections, la plus fréquente."""
    lex = sorted(lire_lexique().values(), key=lambda e: (-e["n"], e.get("vu", "")), reverse=False)
    vus, sortie = set(), []
    for e in lex:
        if e["lu"] in vus:
            continue
        vus.add(e["lu"])
        sortie.append({"lu": e["lu"], "juste": e["juste"], "n": e["n"]})
        if len(sortie) >= n:
            break
    return sortie


def lire_journal() -> list[dict[str, Any]]:
    f = RACINE / BASE / JOURNAL
    if not f.is_file():
        return []
    sortie = []
    for l in f.read_text(encoding="utf-8", errors="ignore").splitlines():
        p = l.split(";", 3)
        if len(p) < 4:
            continue
        meta = dict(re.findall(r"(\w+)=([^,;]*)", p[3]))
        try:
            cer = float(meta.get("cer", "nan"))
        except ValueError:
            continue
        ex = [x for x in meta.get("ex", "").split("|") if x]
        sortie.append({"date": p[0], "dossier": p[1], "nom": p[2], "cer": cer,
                       "exemples": ex, "nb_exemples": len(ex) or int(meta.get("exemples", "0") or 0)})
    return sortie


def scores_exemples(journal: list[dict[str, Any]]) -> tuple[dict[str, dict[str, float]], float | None]:
    """Pour chaque exemple : taux d'erreur moyen des pages transcrites avec lui."""
    sc: dict[str, list[float]] = {}
    for j in journal:
        for e in j["exemples"]:
            sc.setdefault(e, []).append(j["cer"])
    tous = [j["cer"] for j in journal]
    moy = sum(tous) / len(tous) if tous else None
    return {k: {"cer": sum(v) / len(v), "n": len(v)} for k, v in sc.items()}, moy


def base_connaissance(annees: list[dict], corr: list[dict]) -> dict[str, Any]:
    """Exemples possibles (pages validées propres), classés ; on écarte ceux qui dégradent le taux d'erreur."""
    journal = lire_journal()
    sc, moy = scores_exemples(journal)
    cands = []
    for A in annees:
        for M in A["matieres"]:
            for f in M["fiches"]:
                if f["statut"] == "propre" and f["pages"]:
                    cands.append({"dossier": f["dossier"], "nom": f["nom"], "fichier": f["pages"][0]["fichier"],
                                  "page": f["pages"][0]["page"], "indice": 0, "matiere": M["nom"], "propre": f.get("propre", True)})
    for f in corr:
        for k, pg in enumerate(f["pages"]):
            cands.append({"dossier": f["dossier"], "nom": f["nom"], "fichier": pg["fichier"], "page": pg["page"],
                          "indice": k, "matiere": Path(f["dossier"]).name, "propre": True})
    sortie = []
    for c in cands:
        if not c.pop("propre"):
            continue
        cle = c["dossier"] + "/" + c["nom"] + (f"#{c['indice']}" if c["indice"] else "")
        s = sc.get(cle)
        c.update(id=cle, cer=round(s["cer"], 4) if s else None, utilise=s["n"] if s else 0)
        # dégrade : déjà utilisé 3 fois et nettement plus d'erreurs que la moyenne
        c["ecarte"] = bool(s and moy is not None and s["n"] >= 3 and s["cer"] > moy * 1.5 + 0.02)
        sortie.append(c)
    # jamais utilisés d'abord (pour les essayer), puis les meilleurs
    sortie.sort(key=lambda c: (c["ecarte"], c["utilise"] > 0, c["cer"] if c["cer"] is not None else 0))
    return {"exemples": sortie, "lexique": lexique_utile(), "journal": journal[-300:], "cer_moyen": moy}


# ---------- PDF final d'un document ----------
FUSIONS: dict[str, str] = {}
VERROU_FUSION = threading.Lock()


def pdf_final(matiere: Path, doc: str) -> Path:
    return matiere / PDF_FINAL / f"{doc}.pdf"


def etat_document(matiere: Path, doc: str, pages: list[dict]) -> dict[str, Any]:
    texs = [chemin_tex(matiere, p["nom"]) for p in pages]
    valides = sum(1 for p in pages if p["statut"] == "propre")
    final = pdf_final(matiere, doc)
    a_jour = final.is_file() and all(t.is_file() for t in texs) and final.stat().st_mtime >= max(t.stat().st_mtime for t in texs) - 1
    cle = str(final)
    return {"doc": doc, "pages": len(pages), "valides": valides,
            "final": {"fichier": rel(final), "pages": nb_pages_pdf(final), "a_jour": a_jour} if final.is_file() else None,
            "fusion": FUSIONS.get(cle, "")}


def fusionner(matiere: Path, doc: str, pages: list[dict]) -> None:
    """Fabrique PDF/<doc>.pdf avec toutes les pages validées, à la suite (en arrière-plan)."""
    final = pdf_final(matiere, doc)
    cle = str(final)
    with VERROU_FUSION:
        if FUSIONS.get(cle) == "en cours":
            return
        FUSIONS[cle] = "en cours"

    def travail():
        try:
            morceaux, tete = [], None
            for p in pages:
                t = chemin_tex(matiere, p["nom"])
                contenu = t.read_text(encoding="utf-8", errors="ignore")
                if tete is None:
                    tete = contenu[:contenu.find("\\begin{document}")].rstrip()
                    tete = "\n".join(l for l in tete.splitlines() if not l.startswith("%%"))
                c = corps_de(contenu)
                if not re.search(r"^%%\s*page:", c, re.M) and p.get("pages"):
                    pg = p["pages"][0]
                    c = f"%% page: {Path(pg['fichier']).name} (p. {pg['page'] + 1})\n" + c
                morceaux.append(c)
            final.parent.mkdir(exist_ok=True)
            res = compiler_corps(tete, "\n\n".join(morceaux), final, matiere=matiere, nom=doc)
            FUSIONS[cle] = "" if res["ok"] else "erreur : " + res["message"][:200]
        except Exception as e:
            FUSIONS[cle] = f"erreur : {e}"[:200]

    threading.Thread(target=travail, daemon=True).start()


def documents(matiere: Path, fs: list[dict]) -> list[dict]:
    """Un document = un scan (toutes ses pages). Lance la fusion quand tout est validé et que le PDF final manque ou date."""
    groupes: dict[str, list[dict]] = {}
    for f in fs:
        groupes.setdefault(f.get("doc", f["nom"]), []).append(f)
    sortie = []
    for doc, pages in groupes.items():
        e = etat_document(matiere, doc, pages)
        if e["valides"] == e["pages"] and not (e["final"] and e["final"]["a_jour"]) and not e["fusion"]:
            fusionner(matiere, doc, pages)
            e["fusion"] = "en cours"
        sortie.append(e)
    return sortie


def profil() -> str:
    f = RACINE / "mes_notations.txt"
    return f.read_text(encoding="utf-8", errors="ignore") if f.is_file() else ""


# ---------- outils ----------
def arborescence(**_) -> dict[str, Any]:
    if not RACINE.is_dir():
        return {"racine": str(RACINE), "existe": False, "annees": [], "profil": "", "exemples": [], "runs": [], "base": {}}
    try:
        ranger(RACINE)
    except OSError:
        pass
    annees, vus = [], set()
    for a in sous_dossiers(RACINE):
        label, _, nom = a.name.partition(" - ")
        ident = slug(label)
        while ident in vus:
            ident += "-b"
        vus.add(ident)
        matieres = []
        for m in sous_dossiers(a):
            fs = fiches(m)
            try:
                ranger_matiere(m)
            except OSError:
                pass
            for f in fs:
                t = chemin_tex(m, f["nom"])
                f["propre"] = not (t.is_file() and "À VÉRIFIER" in t.read_text(encoding="utf-8", errors="ignore"))
            matieres.append({"id": slug(m.name), "nom": m.name, "fiches": fs, "documents": documents(m, fs)})
        annees.append({"id": ident, "label": label.strip(), "nom": nom.strip(), "matieres": matieres})
    corr = RACINE / CORRECTION
    exemples = [f for f in fiches(corr, valide=True) if f["statut"] == "propre"] if corr.is_dir() else []
    exemples += fiches_calibrage()
    return {"racine": str(RACINE), "existe": True, "annees": annees, "profil": profil(), "exemples": exemples,
            "runs": lire_runs(), "base": base_connaissance(annees, exemples)}


def fiches_calibrage() -> list[dict]:
    """Fiches/_calibrage : chaque scan qui a sa vérité terrain _ocr/<scan>.tex devient un exemple parfait."""
    d = RACINE / CALIBRAGE
    if not d.is_dir():
        return []
    sortie = []
    for f in sorted(d.iterdir(), key=lambda p: p.name.lower()):
        if f.is_file() and f.suffix.lower() in SCANS and trouver_tex(d, f.stem):
            n = nb_pages_pdf(f) if f.suffix.lower() == ".pdf" else 1
            sortie.append({"id": slug(f.stem), "nom": nfc(f.stem), "titre": f.stem.replace("_", " "), "statut": "propre",
                           "dossier": rel(d), "pages": [{"fichier": rel(f), "page": i} for i in range(n)],
                           "pdf": None, "doc": nfc(f.stem), "page_doc": 1, "n_doc": 1})
    return sortie


def lire_scan(fichier: str, page: int = 0, cote: int = COTE_MAX, **_) -> dict[str, Any]:
    from PIL import Image as PIL, ImageOps
    chemin = dans_racine(fichier)
    if chemin.suffix.lower() not in SCANS or not chemin.is_file():
        raise ValueError(f"scan introuvable : {fichier}")
    if chemin.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        with VERROU_PDF:
            doc = pdfium.PdfDocument(str(chemin))
            try:
                if not 0 <= page < len(doc):
                    raise ValueError(f"page {page} absente de {fichier} ({len(doc)} pages)")
                pg = doc[page]
                w, h = pg.get_size()
                im = pg.render(scale=max(2.2, min(cote, 3000) / max(w, h))).to_pil()
            finally:
                doc.close()
    else:
        if chemin.suffix.lower() == ".heic":
            from pillow_heif import register_heif_opener
            register_heif_opener()
        im = ImageOps.exif_transpose(PIL.open(chemin))
    im = im.convert("RGB")
    cote = max(400, min(int(cote), 3000))
    im.thumbnail((cote, cote))
    tampon = io.BytesIO()
    im.save(tampon, "JPEG", quality=85, optimize=True)
    return {"data": tampon.getvalue(), "format": "jpeg"}


def lire_tex(dossier: str, nom: str, **_) -> dict[str, Any]:
    tex = trouver_tex(dans_racine(dossier), nom)
    if not tex:
        return {"existe": False, "corps": "", "statut": "scan"}
    contenu = tex.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"\\begin\{document\}\n?(.*?)\\end\{document\}", contenu, re.S)
    libre = dans_racine(dossier).name in LIBRES
    statut = "propre" if libre else statut_tex(contenu)
    corps = defaire_mise_en_page((m[1] if m else contenu).strip())
    if libre:        # les exemples suivent les règles de la consigne : \titre, jamais \section
        corps = re.sub(r"\\section\*?\{", r"\\titre{", corps)
    return {"existe": True, "corps": corps, "statut": statut}


def enregistrer_tex(dossier: str, nom: str, corps: str, statut: str = "corr", source: str = "", meta: str = "",
                    pdf: bool = False, verifier: bool = False, runs: str = "", brut: str = "", **_) -> dict[str, Any]:
    if runs:          # suivi des runs : lignes JSON ajoutées à _base/runs.jsonl, aucune fiche touchée
        return {"ok": True, "runs_ok": ecrire_runs(runs), "chemin": f"{BASE}/{RUNS}", "statut": statut, "pdf": None, "pdf_ok": False}
    matiere = dans_racine(dossier)
    if not matiere.is_dir() or "/" in nom or "\\" in nom:
        raise ValueError("dossier de matière ou nom de fiche invalide")
    annee = matiere.parent.name.partition(" - ")[0].strip()
    tete = ["\\documentclass[11pt]{article}", "\\usepackage{fiche}",
            f"\\matiere{{{matiere.name}}}", f"\\chapitre{{{annee}}}"]
    if source:
        tete.append(f"\\source{{{Path(source).name}}}")
    if meta:
        tete.append("%% ocr: " + meta.replace("\n", " ")[:300])
    if statut == "propre":
        tete.append("%% statut: propre")
    _, i_page, _ = decouper_nom(nom)
    if verifier:   # on compile le corps à part, sans rien écrire ; renvoie les erreurs et une version qui compile
        res = compiler_corps("\n".join(tete), corps, matiere=matiere, nom=nom, page=i_page - 1)
        return {"ok": True, "verifie": True, "compile": res["ok"], "fatal": bool(res.get("fatal")), "message": res["message"],
                "erreurs": res["erreurs"], "corps": res["corps"].strip(), "statut": statut, "pdf": None, "pdf_ok": False}
    tex = trouver_tex(matiere, nom) or chemin_tex(matiere, nom)
    tex.parent.mkdir(parents=True, exist_ok=True)
    if pdf and not corps.strip():          # corps vide + pdf : on compile le .tex déjà enregistré
        if not tex.is_file():
            raise ValueError("aucune transcription enregistrée pour cette fiche")
        res = compiler(tex)
        return {"ok": True, "chemin": rel(tex), "statut": statut_tex(tex.read_text(encoding="utf-8", errors="ignore")),
                "pdf": infos_pdf(tex) if res["ok"] else None, "pdf_ok": res["ok"], "message": res["message"],
                "corps": res.get("corps", "").strip() if res.get("repare") else ""}
    if statut == "propre":
        corps = nettoyer_valide(corps)            # les \illisible ont été relus
        if brut.strip():                          # base de connaissance : sortie brute gardée + erreurs apprises
            tex.with_suffix(".brut.txt").write_text(brut, encoding="utf-8")
            try:
                ajouter_lexique(erreurs_ocr(brut, corps), matiere.name)
            except Exception:
                pass
    tex.write_text("\n".join(tete) + "\n\n\\begin{document}\n" + corps.strip() + "\n\\end{document}\n", encoding="utf-8")
    if statut == "propre" and meta and "cer=" in meta:
        from datetime import datetime
        ligne = ";".join([datetime.now().strftime("%Y-%m-%d %H:%M"), rel(matiere), nom, meta.replace("\n", " ").replace(";", ",")])
        (RACINE / BASE).mkdir(parents=True, exist_ok=True)
        with open(RACINE / BASE / JOURNAL, "a", encoding="utf-8") as j:
            j.write(ligne + "\n")
    res = compiler(tex) if pdf else {"ok": False, "message": ""}
    retour = {"ok": True, "chemin": rel(tex), "statut": "propre" if statut == "propre" else "corr",
              "pdf": infos_pdf(tex) if res["ok"] else None, "pdf_ok": res["ok"], "message": res["message"],
              "corps": res.get("corps", "").strip() if res.get("repare") else (corps.strip() if statut == "propre" else "")}
    if statut == "propre":                       # dernière page validée : PDF final du document
        try:
            fs = fiches(matiere)
            doc = decouper_nom(nom)[0]
            pages = [f for f in fs if f.get("doc") == doc]
            if pages and all(f["statut"] == "propre" for f in pages):
                fusionner(matiere, doc, pages)
                retour["fusion"] = "en cours"
        except Exception:
            pass
    return retour
