# Fonctionnement détaillé

Ce document décrit ce qui se passe entre le scan et le PDF : le pipeline de transcription, la base de connaissance qui « entraîne » l'OCR, la mesure de qualité et la fabrication du PDF.

Fichiers concernés :
- `tableau/index.html` : interface et orchestration des appels au modèle (fonction `transcrire`) ;
- `serveur/fiches_core.py` : dossiers, scans, base de connaissance, compilation LaTeX ;
- `serveur/latex/fiche.sty` : le style des fiches.

## 1. Une page = une fiche

Un scan de plusieurs pages (PDF ou photos `Nom p1.jpg`, `Nom p2.jpg`…) devient une fiche par page : `SVM (3 sur 6)`. Les premières versions lisaient plusieurs pages par appel. Les pages denses saturaient alors la réponse (texte coupé, pages oubliées, « … » à la place d'un passage). À une page par appel, le problème disparaît, et la continuité entre les pages est rétablie par la fusion finale (§ 6).

Chaque page est rendue en JPEG d'au plus 1 568 px de côté (pdfium pour les PDF, Pillow pour les photos, orientation EXIF corrigée).

## 2. Le pipeline d'une page

```
lecture ──► (suite si coupée) ──► (lecture en deux moitiés si trop dense)
        ──► repérage des dessins sans zone ──► relecture ──► réparation LaTeX ──► compilation d'essai
```

| Étape | Appel | Contenu envoyé |
|---|---|---|
| Lecture | modèle « précis » | consigne + profil + lexique d'erreurs + 2 pages d'exemple (image et LaTeX validé) + la page |
| Suite | idem | si la réponse est tronquée (`max_tokens`) : « continue exactement après le dernier mot » (2 fois au plus) |
| Moitiés | idem | si la page reste trop longue : moitié haute (0 à 55 %) puis moitié basse (45 à 100 %) avec la fin du haut en contexte ; les coordonnées des dessins sont recalées sur la page entière |
| Repérage | modèle standard | les descriptions des `\dessin{…}` sans zone → JSON `[[x1,y1,x2,y2], …]` |
| Relecture | modèle « précis » | la page + la transcription + extraits validés : « compare ligne par ligne, corrige, ne reformule rien » ; acceptée seulement si elle ne raccourcit pas le texte de plus de 15 % |
| Correction LaTeX | modèle « précis » | seulement si la compilation d'essai échoue : erreurs XeLaTeX avec numéros de ligne |

En pratique, une page coûte deux appels (lecture et relecture). Les autres étapes ne servent que pour les pages difficiles.

### La consigne

La consigne (constante `CONSIGNES` dans `index.html`) fixe la fidélité et le format : recopier exactement, fautes comprises ; `\titre`, `\souligne`, `\entoure`, `\couleur{…}`, environnements `definition`, `attention`… ; dessins en `\dessin[x1,y1,x2,y2]{description}` ; texte à côté d'un dessin en `\begin{acote}…\end{acote}` ; mots illisibles en `\illisible{supposition}`. S'y ajoutent des règles de robustesse : LaTeX qui compile, pas de « … », retours à la ligne du scan respectés.

## 3. La base de connaissance

L'« entraînement » est de l'**apprentissage en contexte** : aucun poids n'est modifié. Ce qui change d'une page à l'autre, c'est ce que contient la demande. Tout est rempli automatiquement à la validation d'une page (bouton « Valider », `enregistrer_tex(statut="propre")`).

### 3.1 Profil d'écriture

`Fiches/mes_notations.txt` est écrit à la main une fois, à partir de pages de calibrage. Il liste les ambiguïtés de l'auteur (« mon 2 en exposant ressemble à un λ ; un vrai λ est rare ») et ses abréviations (`°` = « -ion »).

### 3.2 Lexique d'erreurs

À la validation, la sortie brute de l'OCR (`pNN.brut.txt`) est comparée à la version validée :

1. découpage en jetons LaTeX (`\commande`, `\x`, mots, caractères isolés) ;
2. alignement par `difflib.SequenceMatcher` ;
3. seuls les **petits remplacements** sont gardés (au plus 4 jetons de chaque côté, hors ponctuation seule et hors différences d'espaces) ;
4. chaque paire est stockée **avec un jeton de contexte à gauche et à droite**, et comptée dans `_base/lexique.json`.

Le contexte évite des règles absurdes. « 2 → λ » appliqué partout casserait tous les carrés, alors que `$ 2 $` → `$ \lambda $` est une erreur précise. Les 40 erreurs les plus fréquentes (une seule correction par lecture fautive, la plus fréquente) sont ajoutées à la consigne :

```
Erreurs que tu as déjà faites sur cette écriture (…). Vérifie ces cas sur l'image, sans les appliquer à l'aveugle :
« $ 2 $ » -> « $ \lambda $ » (3 fois)
```

### 3.3 Exemples validés

Toute page validée qui ne contient aucune ligne « À VÉRIFIER » devient un exemple possible, comme les documents déposés dans `Fiches/_correction-fiches/`. Deux exemples sont joints à chaque lecture : leur image **et** leur LaTeX validé (la bonne page, découpée aux repères `%% page:`, sans commentaires).

### 3.4 Sélection des exemples, sans dégrader la qualité

Chaque validation écrit une ligne dans `_base/journal_ocr.csv` : date, page, CER, identifiants des exemples utilisés. On en déduit pour chaque exemple $e$ :

$$\overline{\text{CER}}(e) = \frac{1}{|P_e|}\sum_{p \in P_e} \text{CER}(p)$$

où $P_e$ est l'ensemble des pages transcrites avec $e$. Ordre de choix :

1. exemples de la même matière d'abord ;
2. exemples jamais utilisés (exploration), puis du plus petit $\overline{\text{CER}}$ au plus grand ;
3. exemple **écarté** si $|P_e| \geq 3$ et $\overline{\text{CER}}(e) > 1{,}5\,\overline{\text{CER}} + 0{,}02$, où $\overline{\text{CER}}$ est la moyenne générale.

C'est un bandit très simple. La base peut grossir sans limite, mais un exemple qui nuit est retiré dès que la mesure le montre.

### 3.5 Nettoyage à la validation

`\illisible{mot}` devient `mot` : si l'auteur valide, le mot est juste. `\illisible{}` disparaît.

## 4. Mesure : le CER

$$\text{CER} = \frac{d_{\text{Levenshtein}}(\text{sortie OCR},\ \text{page validée})}{|\text{page validée}|}$$

La mesure se fait après normalisation des espaces et sans les repères de page, au moment de la validation. Le panneau « Qualité de l'OCR » montre la moyenne, la tendance (5 premières pages contre 5 dernières), le détail par matière, les erreurs les plus fréquentes et les exemples écartés.

Biais connu : une page validée sans relecture attentive compte 0 %.

## 5. Du LaTeX qui compile toujours

Un modèle de langage produit parfois du LaTeX invalide : accolade oubliée, `$` non fermé, `\section` absente du style, `_` hors maths. Le serveur ne laisse jamais une erreur bloquer le PDF :

1. **Préparation** : suppression des `\clearpage` et des « … », `\section` → `\titre`, `\mathbb{1}` → `\mathbf{1}`, `\souligne` remplacé par `\underline` en mode maths.
2. **Équilibrage** (`equilibrer`) : un automate à pile ferme les accolades, `$`, `\[`, et les `\begin` restés ouverts, et supprime les fermetures orphelines. Une fin de paragraphe ferme ce qui ne peut pas la traverser.
3. **Compilation avec réparation** : XeLaTeX en `-halt-on-error`. À chaque erreur, la ligne fautive, lue dans le log (`l.NN`), est mise de côté : remplacée par une marque rouge « [ligne à vérifier] », avec l'original en commentaire. Puis on recompile, jusqu'à 25 fois.
4. **Débordements** : le log donne les `Overfull \hbox`. Une formule trop large est coupée en `multline*` au meilleur point : connecteur `\text{avec}`, puis relation `=`, `\Rightarrow`…, puis `+`/`−` au niveau 0 des parenthèses, au plus près du milieu. Une accolade de lignes de texte (`\left\{ \begin{array}{l} \text{…}`) devient une boîte où le texte passe à la ligne. Si rien ne marche, la formule est réduite à la largeur de la ligne. Si une coupure casse la compilation, on revient en arrière.

Ces ajustements de mise en page restent hors du `.tex` (`defaire_mise_en_page`). Le fichier garde la forme lisible par l'aperçu et par les exemples.

## 6. Dessins et PDF final

**Dessins.** `\dessin[x1,y1,x2,y2]{…}` : la zone (en % de la page) est découpée dans le scan, avec une marge de 1,5 %. Le fond est blanchi : balance des blancs sur la médiane de la zone, puis seuil. L'image est placée à la même largeur et à la même position horizontale que sur la feuille (`\dessinimg`). Avec `acote`, le dessin et le texte voisin sont mis côte à côte (deux `minipage`), à gauche ou à droite selon la position du dessin sur le scan.

**PDF complet.** Quand toutes les pages d'un document sont validées, le serveur les concatène (repères `%% page:` conservés pour retrouver les dessins) et compile `PDF/<document>.pdf` en arrière-plan. Le texte s'écoule d'une page à l'autre comme dans un seul document, ce qui rétablit la continuité perdue par le découpage page par page.

## 7. Deux façons de faire tourner l'interface

L'interface `tableau/index.html` ne connaît qu'une API : `window.claude.use('mcp')` pour les outils, `window.claude.use('sample')` pour le modèle.

- **Dans claude.ai / Claude Desktop** : la page est un artifact. `mcp` est le serveur MCP local (`serveur_fiches.py`, lancé par Claude Desktop) et `sample` est le modèle de l'abonnement. Claude Desktop n'autorise un appel d'outil que juste après un clic, d'où les boutons « Lire mes dossiers » et « Recharger ».
- **En local avec une clé API** : `app/serveur_web.py` sert la même page et injecte une cale qui implémente ces deux capacités au-dessus de `fiches_core.py` et de l'API Anthropic. `app/streamlit_app.py` l'affiche dans Streamlit.

Le serveur MCP recharge `fiches_core.py` à chaud quand le fichier change. Les outils acceptent un paramètre `extra` (JSON) pour de futures options, sans changer leur signature ni relancer Claude Desktop.
