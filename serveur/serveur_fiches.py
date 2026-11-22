"""Stack local OCR Fiche : serveur MCP lancé par Claude Desktop.

Ce fichier ne fait que déclarer les 4 outils. Tout le travail est dans fiches_core.py, rechargé
automatiquement dès qu'il change : une mise à jour ne demande plus de relancer Claude Desktop.

Outils : arborescence, lire_scan, lire_tex (lecture seule), enregistrer_tex (écriture).
Chaque outil accepte « extra » : options en JSON, transmises telles quelles à fiches_core
(pour ajouter des options plus tard sans changer ce fichier).
Test sans Claude Desktop :  python serveur_fiches.py --test "C:/chemin/vers/Fiches"
"""
import importlib.util
import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

try:                                    # mcp 2.x
    from mcp.server.mcpserver import Image, MCPServer
except ImportError:                     # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer, Image
from mcp.types import ToolAnnotations

ICI = Path(__file__).resolve().parent
CORE = ICI / "fiches_core.py"
_verrou = threading.Lock()
_etat: dict[str, Any] = {"module": None, "mtime": None, "erreur": ""}


def core():
    """fiches_core, rechargé si le fichier a changé. En cas d'erreur, on garde la version précédente."""
    with _verrou:
        try:
            mt = CORE.stat().st_mtime
        except OSError:
            mt = None
        if _etat["module"] is None or mt != _etat["mtime"]:
            try:
                spec = importlib.util.spec_from_file_location("fiches_core", CORE)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _etat.update(module=mod, mtime=mt, erreur="")
            except Exception:
                _etat["erreur"] = traceback.format_exc(limit=3)
                if _etat["module"] is None:
                    raise
        return _etat["module"]


def _opts(extra: str) -> dict[str, Any]:
    try:
        o = json.loads(extra) if extra else {}
        return o if isinstance(o, dict) else {}
    except ValueError:
        return {}


serveur = MCPServer(name="ocr-fiches",
                    instructions="Dossier local des fiches de révision : années, matières, scans et transcriptions LaTeX.")
LECTURE = ToolAnnotations(read_only_hint=True)


@serveur.tool(description="Années, matières et fiches du dossier local, avec le profil d'écriture et la base de connaissance (lecture seule).",
              annotations=LECTURE, structured_output=True)
def arborescence(extra: str = "") -> dict[str, Any]:
    r = core().arborescence(**_opts(extra))
    if _etat["erreur"]:
        r["avertissement"] = "fiches_core.py contient une erreur, l'ancienne version reste utilisée : " + _etat["erreur"][-400:]
    return r


@serveur.tool(description="Une page scannée (ou d'un PDF de fiche), redressée et réduite (JPEG). fichier : chemin relatif "
                          "donné par arborescence ; page : numéro de page dans un PDF (0 pour une image).",
              annotations=LECTURE)
def lire_scan(fichier: str, page: int = 0, cote: int = 1568, extra: str = "") -> Image:
    r = core().lire_scan(fichier, page, cote, **_opts(extra))
    return Image(data=r["data"], format=r.get("format", "jpeg"))


@serveur.tool(description="Transcription LaTeX enregistrée d'une fiche : corps entre \\begin{document} et "
                          "\\end{document}, et statut (lecture seule).",
              annotations=LECTURE, structured_output=True)
def lire_tex(dossier: str, nom: str, extra: str = "") -> dict[str, Any]:
    return core().lire_tex(dossier, nom, **_opts(extra))


@serveur.tool(description="Enregistre la transcription d'une fiche (statut « corr » à corriger ou « propre » validée), "
                          "fabrique son PDF, met à jour la base de connaissance et le suivi.",
              annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
              structured_output=True)
def enregistrer_tex(dossier: str, nom: str, corps: str, statut: str = "corr", source: str = "", meta: str = "",
                    pdf: bool = False, verifier: bool = False, runs: str = "", brut: str = "",
                    extra: str = "") -> dict[str, Any]:
    return core().enregistrer_tex(dossier, nom, corps, statut=statut, source=source, meta=meta, pdf=pdf,
                                  verifier=verifier, runs=runs, brut=brut, **_opts(extra))


if __name__ == "__main__":
    if "--test" in sys.argv:
        i = sys.argv.index("--test")
        if len(sys.argv) > i + 1:
            os.environ["FICHES_RACINE"] = sys.argv[i + 1]
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(core().arborescence(), ensure_ascii=False, indent=2)[:6000])
    else:
        serveur.run()
