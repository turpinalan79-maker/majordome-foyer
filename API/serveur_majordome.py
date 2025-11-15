from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import os
from datetime import datetime
import requests
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

# ---------------------------
# Connexion base de données
# ---------------------------

load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_PASSWORD = os.getenv("DB_PASSWORD")

if not DB_HOST or not DB_PASSWORD:
    raise RuntimeError("DB_HOST ou DB_PASSWORD manquant dans le .env")


def get_db():
    """Retourne une connexion PostgreSQL vers ta base Supabase."""
    return psycopg.connect(
        host=DB_HOST,
        port=5432,
        dbname="postgres",
        user="postgres",
        password=DB_PASSWORD,
        sslmode="require",
    )


# ---------------------------
# Application FastAPI
# ---------------------------

app = FastAPI(title="Majordome Foyer", version="1.2")


# --------- Modèles Pydantic ---------

class Action(BaseModel):
    personne: str   # nom_affiche dans membre
    piece: str      # nom dans piece
    tache: str      # nom dans tache (pour la pièce)
    commentaire: Optional[str] = None


# --------- Endpoints simples ---------

@app.get("/health")
async def health():
    return {"status": "ok"}


# --------- Pièces ---------

@app.get("/pieces")
async def liste_pieces():
    """
    Liste des pièces (schéma Supabase réel).
    """
    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    id_piece AS id,
                    nom,
                    superficie_m2,
                    etage,
                    exposition,
                    type_sol
                FROM piece
                ORDER BY nom;
                """
            )
            return cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lecture pièces : {e}")


# --------- Tâches prioritaires ---------

@app.get("/taches/prioritaires")
async def taches_prioritaires(jours: int = 7):
    """
    Retourne les tâches avec un score de priorité.

    - priorite_hygiene : 1 à 5 (obligatoire)
    - regle.priorite_base : priorisation générale de la règle (par défaut 50)

    Exemple de score :
        score = COALESCE(regle.priorite_base, 50) + 10 * priorite_hygiene
    """
    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    t.id_tache,
                    t.nom AS tache,
                    p.nom AS piece,
                    t.frequence,
                    t.interval_jours,
                    t.priorite_hygiene,
                    r.priorite_base,
                    COALESCE(r.priorite_base, 50)
                        + 10 * COALESCE(t.priorite_hygiene, 0) AS score_priorite
                FROM tache t
                JOIN piece p ON p.id_piece = t.id_piece
                LEFT JOIN regle r
                    ON r.id_tache = t.id_tache
                   AND r.active = true
                ORDER BY score_priorite DESC, piece, tache;
                """
            )
            return cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lecture tâches : {e}")


# --------- Journalisation d'une action ---------

@app.post("/actions")
async def enregistrer_action(action: Action):
    """
    Enregistre une action dans la table public.action.

    - personne -> membre.nom_affiche (id_membre)
    - piece    -> piece.nom (id_piece)
    - tache    -> tache.nom pour cette pièce (id_tache)
    - statut   -> 'faite' par défaut
    - origine  -> 'API_MAJORDOME'
    """
    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:

            # 1) Résoudre la pièce
            cur.execute(
                "SELECT id_piece FROM piece WHERE nom = %s;",
                (action.piece,)
            )
            row_piece = cur.fetchone()
            if not row_piece:
                raise HTTPException(
                    status_code=404,
                    detail=f"Pièce inconnue : '{action.piece}'"
                )
            id_piece = row_piece["id_piece"]

            # 2) Résoudre la tâche pour cette pièce
            cur.execute(
                """
                SELECT id_tache
                FROM tache
                WHERE id_piece = %s AND nom = %s;
                """,
                (id_piece, action.tache)
            )
            row_tache = cur.fetchone()
            if not row_tache:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"Tâche inconnue '{action.tache}' "
                        f"pour la pièce '{action.piece}'"
                    ),
                )
            id_tache = row_tache["id_tache"]

            # 3) Résoudre le membre (facultatif)
            cur.execute(
                "SELECT id_membre FROM membre WHERE nom_affiche = %s;",
                (action.personne,)
            )
            row_membre = cur.fetchone()
            id_membre = row_membre["id_membre"] if row_membre else None

            # 4) Insertion dans action
            cur.execute(
                """
                INSERT INTO action (
                    id_membre,
                    id_piece,
                    id_tache,
                    statut,
                    commentaire,
                    origine
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id_action, horodatage_utc;
                """,
                (
                    id_membre,
                    id_piece,
                    id_tache,
                    "faite",                    # statut par défaut
                    action.commentaire,
                    "API_MAJORDOME",
                ),
            )
            inserted = cur.fetchone()
            conn.commit()

        return {
            "ok": True,
            "id_action": inserted["id_action"],
            "horodatage_utc": inserted["horodatage_utc"],
            "id_membre": id_membre,
            "id_piece": id_piece,
            "id_tache": id_tache,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur enregistrement action : {e}")


# --------- Configuration foyer & météo ---------

def _get_foyer_config():
    """
    Lit la table foyer_config (id = 1) pour récupérer ville / lat / lon.
    """
    with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT ville, lat, lon FROM foyer_config WHERE id = 1;")
        row = cur.fetchone()
        if not row:
            return {"ville": "Inconnue", "lat": None, "lon": None}
        return row


def _get_meteo(lat: float, lon: float):
    """
    Récupère la météo du jour via l'API Open-Meteo.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": (
            "temperature_2m_min,temperature_2m_max,"
            "windspeed_10m_max,precipitation_sum"
        ),
        "forecast_days": 1,
        "timezone": "Europe/Paris",
    }
    r = requests.get(url, params=params, timeout=5)
    r.raise_for_status()
    return r.json()


@app.get("/alertes/suggestions")
async def alertes_suggestions():
    """
    Utilise la météo pour générer des alertes intelligentes :
      - gel : protéger les plantes
      - vent fort : ranger jardin / voiture au garage
      - grosse pluie : surveiller extérieur
      - forte chaleur : fermer volets exposés, etc.
    """
    cfg = _get_foyer_config()
    ville = cfg["ville"]
    lat = cfg["lat"]
    lon = cfg["lon"]

    if lat is None or lon is None:
        return {
            "ville": ville,
            "alertes": [],
            "info": (
                "Coordonnées absentes dans foyer_config, "
                "impossible de récupérer la météo."
            ),
        }

    alertes: List[str] = []
    info = ""

    try:
        meteo = _get_meteo(lat, lon)
        daily = meteo.get("daily", {})
        tmin = daily.get("temperature_2m_min", [None])[0]
        tmax = daily.get("temperature_2m_max", [None])[0]
        windmax = daily.get("windspeed_10m_max", [None])[0]
        pluie = daily.get("precipitation_sum", [None])[0]

        info = (
            f"Tmin={tmin}°C, Tmax={tmax}°C, "
            f"vent max={windmax} km/h, pluie={pluie} mm."
        )

        if tmin is not None and tmin <= 0:
            alertes.append("Rentrer ou protéger les plantes sensibles au gel.")
        if windmax is not None and windmax >= 60:
            alertes.append(
                "Ranger ce qui traîne dans le jardin et mettre la voiture au garage."
            )
        if pluie is not None and pluie >= 10:
            alertes.append(
                "Vérifier les gouttières et éviter d'étendre du linge dehors."
            )
        if tmax is not None and tmax >= 28:
            alertes.append(
                "Fermer les volets exposés au soleil dans l'après-midi "
                "pour garder la fraîcheur."
            )

    except Exception as e:
        info = f"Impossible de récupérer la météo : {e}"

    return {
        "ville": ville,
        "alertes": alertes,
        "info": info,
    }
