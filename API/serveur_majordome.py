from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import os
from datetime import datetime
import requests
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

# Chargement du .env en local (inutile sur Render mais ne gêne pas)
load_dotenv()

# --- Variables d environnement pour la base Supabase (via pooler) ---

DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD")

if not DB_HOST or not DB_PASSWORD:
    raise RuntimeError("DB_HOST ou DB_PASSWORD manquant dans le .env / les variables d environnement")


def get_db():
    """Retourne une connexion PostgreSQL vers ta base Supabase (pooler)."""
    return psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        sslmode="require",
    )


app = FastAPI(title="Majordome Foyer", version="1.3")

# --------- Modèles ---------


class Action(BaseModel):
    personne: str
    piece: str
    tache: str
    commentaire: Optional[str] = None


class NouvelleTache(BaseModel):
    piece: str                         # Nom de la piece (doit exister dans piece.nom)
    tache: str                         # Nom de la tache
    frequence: str                     # Champ tache.frequence (ex: "quotidienne", "hebdomadaire")
    interval_jours: Optional[int] = None  # Champ tache.interval_jours
    priorite_hygiene: int = 3          # 1 a 5

    periodicite: Optional[str] = None          # regle.periodicite (si None, on copie frequence)
    regle_intervalle_jours: Optional[int] = None  # regle.intervalle_jours (si None, on copie interval_jours)
    jour_semaine: Optional[str] = None         # ex: "lundi", "dimanche", ou NULL
    priorite_base: int = 50                    # regle.priorite_base

    eviter_pluie: bool = False
    eviter_vent: bool = False
    eviter_neige: bool = False
    eviter_gel: bool = False
    eviter_nuit: bool = False


# --------- Endpoints simples ---------


@app.get("/health")
async def health():
    return {"status": "ok"}


# --------- Pièces ---------


@app.get("/pieces")
async def liste_pieces():
    """
    Liste des pieces (table piece).
    """
    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT
                    id_piece AS id,
                    nom,
                    superficie_m2,
                    etage,
                    exposition,
                    type_sol
                FROM piece
                ORDER BY nom;
            """)
            return cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lecture pieces : {e}")


# --------- Tâches prioritaires ---------


@app.get("/taches/prioritaires")
async def taches_prioritaires(jours: int = 7):
    """
    Retourne les taches avec un score de priorite simple,
    en excluant celles faites dans les `jours` derniers jours.
    """
    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT
                    t.id_tache,
                    t.nom AS tache,
                    p.nom AS piece,
                    r.periodicite,
                    r.intervalle_jours,
                    r.jour_semaine,
                    r.priorite_base,
                    t.priorite_hygiene,
                    (COALESCE(r.priorite_base, 0) + COALESCE(t.priorite_hygiene, 0)) AS score_priorite
                FROM tache t
                JOIN piece p ON p.id_piece = t.id_piece
                LEFT JOIN regle r ON r.id_tache = t.id_tache
                LEFT JOIN action a
                  ON a.id_tache = t.id_tache
                 AND a.statut = 'faite'
                 AND a.horodatage_utc >= NOW() - (%s * INTERVAL '1 day')
                WHERE COALESCE(r.active, TRUE) = TRUE
                  AND a.id_action IS NULL
                ORDER BY score_priorite DESC, piece, tache;
            """, (jours,))
            return cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lecture taches : {e}")


# --------- Création d une tâche ---------


@app.post("/taches")
async def creer_tache(nouvelle: NouvelleTache):
    """
    Cree une nouvelle tache de menage :
      - verifie que la piece existe,
      - insere dans tache,
      - insere la regle associee dans regle.
    """
    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            # 1) Recuperer la piece
            cur.execute(
                "SELECT id_piece FROM piece WHERE nom = %s;",
                (nouvelle.piece,)
            )
            row_piece = cur.fetchone()
            if not row_piece:
                raise HTTPException(status_code=404, detail="Piece inconnue: " + nouvelle.piece)
            id_piece = row_piece["id_piece"]

            # 2) Determiner les valeurs pour la regle
            periodicite_regle = nouvelle.periodicite or nouvelle.frequence
            intervalle_regle = (
                nouvelle.regle_intervalle_jours
                if nouvelle.regle_intervalle_jours is not None
                else nouvelle.interval_jours
            )

            # 3) Inserer la tache
            cur.execute("""
                INSERT INTO tache (
                    nom,
                    id_piece,
                    frequence,
                    interval_jours,
                    priorite_hygiene,
                    eviter_pluie,
                    eviter_vent,
                    eviter_neige,
                    eviter_gel,
                    eviter_nuit
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id_tache, nom, id_piece, frequence, interval_jours, priorite_hygiene;
            """, (
                nouvelle.tache,
                id_piece,
                nouvelle.frequence,
                nouvelle.interval_jours,
                nouvelle.priorite_hygiene,
                nouvelle.eviter_pluie,
                nouvelle.eviter_vent,
                nouvelle.eviter_neige,
                nouvelle.eviter_gel,
                nouvelle.eviter_nuit,
            ))
            row_tache = cur.fetchone()
            id_tache = row_tache["id_tache"]

            # 4) Inserer la regle associee
            cur.execute("""
                INSERT INTO regle (
                    id_piece,
                    id_tache,
                    periodicite,
                    intervalle_jours,
                    jour_semaine,
                    priorite_base,
                    active
                )
                VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                RETURNING id_regle, periodicite, intervalle_jours, jour_semaine, priorite_base, active;
            """, (
                id_piece,
                id_tache,
                periodicite_regle,
                intervalle_regle,
                nouvelle.jour_semaine,
                nouvelle.priorite_base,
            ))
            row_regle = cur.fetchone()

            conn.commit()

        return {
            "ok": True,
            "tache": row_tache,
            "regle": row_regle,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur creation tache : {e}")


# --------- Journalisation d une action ---------


@app.post("/actions")
async def enregistrer_action(action: Action):
    """
    Enregistre une action dans la table action.
    On cherche la piece et la tache par nom.
    """
    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            # Récuperer id_piece
            cur.execute(
                "SELECT id_piece FROM piece WHERE nom = %s;",
                (action.piece,)
            )
            row_piece = cur.fetchone()
            if not row_piece:
                raise HTTPException(status_code=404, detail="Piece inconnue: " + action.piece)
            id_piece = row_piece["id_piece"]

            # Récuperer id_tache
            cur.execute(
                "SELECT id_tache FROM tache WHERE nom = %s AND id_piece = %s;",
                (action.tache, id_piece)
            )
            row_tache = cur.fetchone()
            if not row_tache:
                raise HTTPException(status_code=404, detail="Tache inconnue pour cette piece: " + action.tache)
            id_tache = row_tache["id_tache"]

            # (optionnel) recuperer un membre "par defaut"
            cur.execute(
                "SELECT id_membre FROM membre WHERE nom_affiche = %s;",
                (action.personne,)
            )
            row_membre = cur.fetchone()
            id_membre = row_membre["id_membre"] if row_membre else None

            cur.execute("""
                INSERT INTO action (horodatage_utc, id_membre, id_piece, id_tache, statut, commentaire, origine)
                VALUES (NOW(), %s, %s, %s, %s, %s, %s)
                RETURNING id_action, horodatage_utc, id_membre, id_piece, id_tache;
            """, (
                id_membre,
                id_piece,
                id_tache,
                "faite",
                action.commentaire,
                "api_majordome",
            ))
            inserted = cur.fetchone()
            conn.commit()

        return {"ok": True, **inserted}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur enregistrement action : {e}")


# --------- Configuration foyer & météo ---------


def _get_foyer_config():
    """
    Lit la table foyer_config (id = 1) pour recuperer ville / lat / lon.
    """
    with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT ville, lat, lon FROM foyer_config WHERE id = 1;")
        row = cur.fetchone()
        if not row:
            return {"ville": "Inconnue", "lat": None, "lon": None}
        return row


def _get_meteo(lat: float, lon: float):
    """
    Recupere la meteo du jour via l API Open-Meteo.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_min,temperature_2m_max,windspeed_10m_max,precipitation_sum",
        "forecast_days": 1,
        "timezone": "Europe/Paris",
    }
    r = requests.get(url, params=params, timeout=5)
    r.raise_for_status()
    return r.json()


@app.get("/alertes/suggestions")
async def alertes_suggestions():
    """
    Utilise la meteo pour generer des alertes intelligentes.
    """
    cfg = _get_foyer_config()
    ville = cfg["ville"]
    lat = cfg["lat"]
    lon = cfg["lon"]

    if lat is None or lon is None:
        return {
            "ville": ville,
            "alertes": [],
            "info": "Coordonnees absentes dans foyer_config, impossible de recuperer la meteo."
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

        info = f"Tmin={tmin}°C, Tmax={tmax}°C, vent max={windmax} km/h, pluie={pluie} mm."

        if tmin is not None and tmin <= 0:
            alertes.append("Rentrer ou proteger les plantes sensibles au gel.")
        if windmax is not None and windmax >= 60:
            alertes.append("Ranger ce qui traine dans le jardin et mettre la voiture au garage.")
        if pluie is not None and pluie >= 10:
            alertes.append("Verifier les gouttieres et eviter d etendre du linge dehors.")
        if tmax is not None and tmax >= 28:
            alertes.append("Fermer les volets exposes au soleil dans l apres-midi pour garder la fraicheur.")

    except Exception as e:
        info = f"Impossible de recuperer la meteo : {e}"

    return {
        "ville": ville,
        "alertes": alertes,
        "info": info,
    }
