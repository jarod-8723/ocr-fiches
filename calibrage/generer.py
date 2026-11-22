"""Fabrique calibrage.tex (style des fiches) à partir de calibrage.txt."""
import re
import sys
from pathlib import Path

ICI = Path(__file__).parent
SYM = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta", "ε": r"\varepsilon", "ζ": r"\zeta", "η": r"\eta",
    "θ": r"\theta", "ι": r"\iota", "κ": r"\kappa", "λ": r"\lambda", "μ": r"\mu", "ν": r"\nu", "ξ": r"\xi", "π": r"\pi",
    "ρ": r"\rho", "σ": r"\sigma", "τ": r"\tau", "υ": r"\upsilon", "φ": r"\varphi", "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega",
    "Γ": r"\Gamma", "Δ": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda", "Ξ": r"\Xi", "Π": r"\Pi", "Σ": r"\Sigma",
    "Φ": r"\Phi", "Ψ": r"\Psi", "Ω": r"\Omega", "−": "-", "×": r"\times", "÷": r"\div", "·": r"\cdot", "±": r"\pm",
    "≠": r"\neq", "≈": r"\approx", "≃": r"\simeq", "≡": r"\equiv", "∝": r"\propto", "≤": r"\leq", "≥": r"\geq",
    "≪": r"\ll", "≫": r"\gg", "∑": r"\sum", "∏": r"\prod", "∫": r"\int", "∬": r"\iint", "∮": r"\oint", "∂": r"\partial",
    "∇": r"\nabla", "√": r"\surd", "∞": r"\infty", "∈": r"\in", "∉": r"\notin", "⊂": r"\subset", "⊆": r"\subseteq",
    "⊄": r"\not\subset", "∪": r"\cup", "∩": r"\cap", "∖": r"\setminus", "∅": r"\emptyset", "∀": r"\forall",
    "∃": r"\exists", "∄": r"\nexists", "¬": r"\neg", "∧": r"\wedge", "∨": r"\vee", "→": r"\to", "←": r"\leftarrow",
    "↔": r"\leftrightarrow", "⇒": r"\Rightarrow", "⇐": r"\Leftarrow", "⇔": r"\Leftrightarrow", "↦": r"\mapsto",
    "↑": r"\uparrow", "↓": r"\downarrow", "ℓ": r"\ell", "²": "^2", "⁻³": "^{-3}", "<": "<", ">": ">", "|": r"\vert",
}
TEXTE = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "#": r"\#", "%": r"\%", "&": r"\&",
         "_": r"\_", "^": r"\^{}", "~": r"\~{}", "‰": r"{\fontspec{Latin Modern Sans}‰}", "€": r"\texteuro{}"}


def texte(seg: str, symboles: bool = True) -> str:
    sortie = []
    i = 0
    while i < len(seg):
        if seg.startswith("⁻³", i):
            sortie.append("$^{-3}$"); i += 2; continue
        c = seg[i]
        if c in TEXTE:
            sortie.append(TEXTE[c])
        elif symboles and c in SYM and c not in "<>|" or c in "<>":
            sortie.append(f"${SYM.get(c, c)}$")
        else:
            sortie.append(c)
        i += 1
    return "".join(sortie)


def ligne(l: str, symboles: bool) -> str:
    if l.startswith("\\["):
        return l.replace("\\mathbb{1}", "\\mathbf{1}")
    morceaux = re.split(r"(?<!\\)\$", l)
    if len(morceaux) % 2 == 0:          # un $ isolé : c'est du texte (ligne de ponctuation)
        return texte(l, symboles)
    return "".join("$" + m.replace("\\mathbb{1}", "\\mathbf{1}") + "$" if k % 2 else texte(m, symboles) for k, m in enumerate(morceaux))


def generer(src: Path) -> str:
    pages, cur = [], None
    for l in src.read_text(encoding="utf-8").splitlines():
        m = re.match(r"# =+ PAGE (\d+) : (.*)$", l)
        if m:
            cur = {"n": m[1], "titre": m[2], "lignes": []}
            pages.append(cur)
        elif cur is not None and l.strip():
            cur["lignes"].append(l)
    out = [r"""\documentclass[11pt]{article}
\usepackage{fiche}
\matiere{Calibrage de l'écriture}
\chapitre{OCR Fiches}
\newcommand{\aecrire}[1]{\par\noindent\begin{minipage}{\linewidth}\raggedright #1\end{minipage}\par\vspace{2pt}{\color{fiche@roseclair}\rule{\linewidth}{.6pt}}\par\vspace{5pt}}
\newcommand{\consigne}[1]{\par\noindent{\color{fiche@gris}\small\itshape\ding{46}~#1}\par\vspace{4pt}}
\begin{document}
\titre{\entoure{\ding{72}} \souligne{Calibrage de l'écriture}}

Ces 8 pages servent à apprendre ton écriture à l'OCR. Tu les recopies à la main, l'OCR lit tes feuilles, et comme le texte d'origine est connu, chaque différence entre sa lecture et ce texte lui apprend comment tu traces chaque lettre, chaque chiffre et chaque symbole.

\begin{methode}[Comment faire]
\begin{itemize}
\item Écris comme d'habitude : même stylo, même taille, même vitesse. Ne t'applique pas plus que sur une vraie fiche.
\item Une page de ce document = une feuille blanche. Si ça déborde, continue sur une autre feuille.
\item Scanne chaque feuille dans \texttt{Fiches/\_calibrage/} : \texttt{calibrage\_p1.jpg}, \texttt{calibrage\_p2.jpg}… (\texttt{calibrage\_p3b.jpg} pour une suite).
\item Garde l'ordre des lignes. Si une ligne est trop longue pour ta feuille, continue à la ligne suivante.
\item Les consignes en gris (\ding{46}) ne s'écrivent pas.
\item Les formules s'écrivent comme en cours : recopie ce que tu vois, pas du code.
\item Si tu te trompes, barre et continue : les ratures sont ignorées.
\end{itemize}
\end{methode}

\begin{attention}
Pas le temps de tout faire ? Commence par les pages 1, 4, 5 et 7, et refais la page 7 sur une feuille à carreaux.
\end{attention}

\begin{remarque}
Le texte de référence est dans \texttt{calibrage.txt} : tu n'as rien à recopier au clavier. L'OCR compare sa lecture de tes feuilles à ce texte, en déduit tes ambiguïtés (« mon 2 ressemble à un $\lambda$ ») pour \texttt{mes\_notations.txt}, et tes feuilles deviennent des exemples parfaits pour les prochaines lectures.
\end{remarque}
"""]
    for p in pages:
        out.append(f"\n\\clearpage\n\\titre{{\\entoure{{{p['n']}}} \\souligne{{{texte(p['titre'][0].upper() + p['titre'][1:])}}}}}\n")
        tableau = [l for l in p["lignes"] if "|" in l and not l.startswith(("#", "$", "l 1", "\\["))]
        for l in p["lignes"]:
            if l in tableau:
                continue
            if l.startswith("#"):
                out.append("\\consigne{" + texte(l.lstrip("# ").strip()) + "}")
            else:
                out.append("\\aecrire{" + ligne(l, symboles=True) + "}")
        if tableau:
            cols = len(tableau[0].split("|"))
            out.append("\\begin{center}\\renewcommand{\\arraystretch}{1.5}\\begin{tabular}{|" + "c|" * cols + "}\\hline")
            for k, l in enumerate(tableau):
                cases = [texte(c.strip()) for c in l.split("|")]
                out.append(" & ".join(f"\\textbf{{{c}}}" if k == 0 else c for c in cases) + " \\\\ \\hline")
            out.append("\\end{tabular}\\end{center}")
    out.append("\n\\end{document}\n")
    return "\n".join(out)


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ICI / "calibrage.txt"
    (ICI / "calibrage.tex").write_text(generer(src), encoding="utf-8")
    print("calibrage.tex écrit")
