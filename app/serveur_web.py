"""Le Tableau des fiches en local, sans Claude Desktop : le même index.html, servi par un petit serveur HTTP.

Dans claude.ai, la page parle à Claude et au dossier local par `window.claude` (capacités `mcp` et `sample`).
Ici, une cale (`CALE` ci-dessous) recrée ce même `window.claude` :
    claude.use('mcp')    -> POST /api/outil  -> fiches_core (les mêmes fonctions que le serveur MCP)
    claude.use('sample') -> POST /api/claude -> API Anthropic (ta clé ANTHROPIC_API_KEY)
La page n'est donc pas modifiée : même interface, même pipeline d'OCR.

    python app/serveur_web.py --fiches "C:/chemin/vers/Fiches"      puis ouvrir http://localhost:8765
    OCR_SIMULATION=1 : réponses de Claude simulées (pour tester sans clé ni dépense).
"""
import argparse
import base64
import importlib.util
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ICI = Path(__file__).resolve().parent
RACINE_DEPOT = ICI.parent
TABLEAU = RACINE_DEPOT / "tableau" / "index.html"
CORE = RACINE_DEPOT / "serveur" / "fiches_core.py"

# Niveaux demandés par la page -> modèles de l'API (modifiables par variables d'environnement)
MODELES = {
    "complex": os.environ.get("OCR_MODELE_PRECIS", "claude-opus-5-5"),
    "default": os.environ.get("OCR_MODELE", "claude-sonnet-5-5"),
    "quick": os.environ.get("OCR_MODELE_RAPIDE", "claude-haiku-5-5"),
}

_core = None


def core():
    global _core
    if _core is None:
        spec = importlib.util.spec_from_file_location("fiches_core", CORE)
        _core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_core)
    return _core


def outil(nom: str, args: dict) -> dict:
    """Même forme de réponse que l'outil MCP vu par la page."""
    c = core()
    if nom == "arborescence":
        return {"payload": c.arborescence()}
    if nom == "lire_tex":
        return {"payload": c.lire_tex(**args)}
    if nom == "enregistrer_tex":
        return {"payload": c.enregistrer_tex(**args)}
    if nom == "lire_scan":
        r = c.lire_scan(args["fichier"], int(args.get("page", 0)), int(args.get("cote", 1568)))
        return {"content": [{"type": "image", "data": base64.b64encode(r["data"]).decode(), "mimeType": "image/jpeg"}]}
    raise ValueError(f"outil inconnu : {nom}")


def claude(demande: dict) -> dict:
    """Un appel « sample » de la page -> un message à l'API Anthropic (images d'abord, puis la consigne)."""
    niveau = demande.get("niveau") or "default"
    if os.environ.get("OCR_SIMULATION") == "1":
        return {"text": "\\titre{Page simulée}\nTexte simulé : $\\sigma^2 = 1$, une transcription de démonstration assez longue.",
                "truncated": False, "modelTierApplied": niveau}
    import anthropic
    cle = os.environ.get("ANTHROPIC_API_KEY")
    if not cle:
        raise PermissionError("ANTHROPIC_API_KEY manquante")
    contenu = [{"type": "image", "source": {"type": "base64", "media_type": im.get("type") or "image/jpeg", "data": im["data"]}}
               for im in demande.get("images", [])]
    contenu.append({"type": "text", "text": demande["texte"]})
    client = anthropic.Anthropic(api_key=cle)
    r = client.messages.create(model=MODELES.get(niveau, MODELES["default"]), max_tokens=12000,
                               messages=[{"role": "user", "content": contenu}])
    texte = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    return {"text": texte, "truncated": r.stop_reason == "max_tokens", "modelTierApplied": niveau,
            "usage": {"in": r.usage.input_tokens, "out": r.usage.output_tokens}}


# Cale injectée dans la page : recrée window.claude.use('mcp' | 'sample') au-dessus de ce serveur.
CALE = r"""<script>
(() => {
  const post = (url, corps, signal) => fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(corps), signal})
    .then(async r => { const j = await r.json(); if (!r.ok) throw {code: j.code || 'tool_error', message: j.message || r.statusText}; return j; });
  const b64 = b => new Promise((ok, ko) => { const f = new FileReader(); f.onload = () => ok(String(f.result).split(',')[1]); f.onerror = ko; f.readAsDataURL(b); });
  const mcp = {
    callTool: (serveur, nom, args) => post('/api/outil', {nom, args: args || {}}),
    watchTool(serveur, nom, args, cb, opts) {
      const go = () => post('/api/outil', {nom, args: args || {}}).then(r => cb({type: 'data', result: r}), e => cb({type: 'error', error: e}));
      go(); const t = setInterval(go, (opts && opts.refetchInterval) || 60000); return () => clearInterval(t);
    },
    invalidate: async () => {}
  };
  const sample = async (texte, o = {}) => {
    if (typeof texte !== 'string') texte = texte.map(m => m.content).join('\n\n');
    const images = await Promise.all([...(o.images || [])].map(async b => ({type: b.type || 'image/jpeg', data: await b64(b)})));
    try {
      const r = await post('/api/claude', {texte, images, niveau: o.modelTier}, o.signal);
      if (o.onText) o.onText({text: r.text, delta: r.text});
      return r;
    } catch (e) { if (o.signal && o.signal.aborted) throw {code: 'cancelled'}; throw e; }
  };
  sample.limits = async () => ({images: {maxCount: 20}, maxPromptBytes: 262144});
  window.claude = {use: async n => n === 'mcp' ? mcp : n === 'sample' ? sample : null};
})();
</script>"""


class Gestion(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/index.html"):
            return self._json(404, {"message": "introuvable"})
        html = TABLEAU.read_text(encoding="utf-8").replace("<script>", CALE + "\n<script>", 1)
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        try:
            demande = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api/outil":
                return self._json(200, outil(demande["nom"], demande.get("args") or {}))
            if self.path == "/api/claude":
                return self._json(200, claude(demande))
            return self._json(404, {"message": "introuvable"})
        except PermissionError as e:
            return self._json(401, {"code": "not_granted", "message": str(e)})
        except Exception as e:
            code = "rate_limited" if "rate" in type(e).__name__.lower() else "tool_error"
            return self._json(500, {"code": code, "message": f"{type(e).__name__} : {e}"})


def demarrer(fiches: str, port: int = 8765) -> ThreadingHTTPServer:
    os.environ["FICHES_RACINE"] = str(Path(fiches).expanduser().resolve())
    global _core
    _core = None
    srv = ThreadingHTTPServer(("127.0.0.1", port), Gestion)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Tableau des fiches en local (API Anthropic)")
    p.add_argument("--fiches", default=os.environ.get("FICHES_RACINE", str(RACINE_DEPOT / "exemples" / "Fiches")))
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args()
    srv = demarrer(a.fiches, a.port)
    print(f"Tableau des fiches : http://localhost:{a.port}  (dossier : {os.environ['FICHES_RACINE']})")
    if "--sans-navigateur" not in sys.argv:
        webbrowser.open(f"http://localhost:{a.port}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
