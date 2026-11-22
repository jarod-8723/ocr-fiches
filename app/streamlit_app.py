"""Le Tableau des fiches dans Streamlit, avec ta clé API Anthropic.

    streamlit run app/streamlit_app.py

Streamlit gère la configuration (clé, dossier des fiches) ; l'interface est le même tableau que dans claude.ai,
servi par app/serveur_web.py sur http://localhost:8765 et affiché pleine largeur.
La clé vient de la barre latérale, de st.secrets["ANTHROPIC_API_KEY"] ou de la variable d'environnement.
"""
import os
import socket
import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

sys.path.insert(0, str(Path(__file__).resolve().parent))
import serveur_web  # noqa: E402

st.set_page_config(page_title="Tableau des fiches", page_icon="★", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""<style>
.block-container{padding:0 !important;max-width:100% !important}
header[data-testid="stHeader"]{background:transparent}
[data-testid="stToolbar"],[data-testid="stDecoration"]{display:none}
iframe{border:0;display:block}
</style>""", unsafe_allow_html=True)


def cle_secrets() -> str:
    try:
        return st.secrets.get("ANTHROPIC_API_KEY", "")
    except Exception:
        return ""


with st.sidebar:
    st.header("Réglages")
    cle = st.text_input("Clé API Anthropic", type="password", value=os.environ.get("ANTHROPIC_API_KEY") or cle_secrets(),
                        help="Elle reste sur ta machine : elle n'est envoyée qu'à l'API Anthropic.")
    fiches = st.text_input("Dossier des fiches", value=os.environ.get("FICHES_RACINE", str(serveur_web.RACINE_DEPOT / "exemples" / "Fiches")))
    simulation = st.toggle("Mode simulation (sans appel à l'API)", value=os.environ.get("OCR_SIMULATION") == "1")
    hauteur = st.slider("Hauteur du tableau", 600, 1400, 900, 50)
    st.caption("Modèles : précis = " + serveur_web.MODELES["complex"] + ", standard = " + serveur_web.MODELES["default"])

if cle:
    os.environ["ANTHROPIC_API_KEY"] = cle
os.environ["OCR_SIMULATION"] = "1" if simulation else "0"


def port_libre(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


@st.cache_resource
def serveur(dossier: str):
    port = 8765
    while not port_libre(port):
        port += 1
    serveur_web.demarrer(dossier, port)
    return port


if not Path(fiches).expanduser().is_dir():
    st.error(f"Dossier introuvable : {fiches}")
    st.stop()
if not cle and not simulation:
    st.info("Ajoute ta clé API Anthropic dans la barre latérale (ou active le mode simulation) pour transcrire. "
            "Tu peux déjà parcourir tes fiches.")

port = serveur(str(Path(fiches).expanduser().resolve()))
url = f"http://localhost:{port}/"
if hasattr(st, "iframe"):          # Streamlit récent
    st.iframe(url, height=hauteur)
else:
    components.iframe(url, height=hauteur, scrolling=True)
