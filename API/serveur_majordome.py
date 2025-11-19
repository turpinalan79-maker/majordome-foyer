from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict
import os
from datetime import datetime
import requests
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

# Chargement du .env en local (inutile sur Render mais ne gene pas)
load_dotenv()

# --- CONFIG DATABASE ---
DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD")

if not DB_HOST or not DB_PASSWORD:
    raise RuntimeError("Variables d'environnement DB manquantes (DB_HOST / DB_PASSWORD).")

def get_db():
    """Connexion PostgreSQL vers ta base Supabase (pooler)."""
    return psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        sslmode="require",
    )


app = FastAPI(title="Majordome Foyer", version="3.0-hybride")

# --------- MODELES Pydantic ---------

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


# --------- OUTILS LOGIQUES ---------

# 0 = lundi ... 6 = dimanche
JOURS_MAP = {
    "lundi": 0,
    "mardi": 1,
    "mercredi": 2,
    "jeudi": 3,
    "vendredi": 4,
    "samedi": 5,
    "dimanche": 6,
}

def _get_meteo_data(lat: float, lon: float) -> Dict:
    """Récupère la météo brute (daily) depuis Open-Meteo."""
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_min,windspeed_10m_max,precipitation_sum",
            "forecast_days": 1,
            "timezone": "Europe/Paris",
        }
        r = requests.get(url, params=params, timeout=5)
        r.raise_for_status()
        return r.json().get("daily", {})
    except Exception:
        return {}

def _calculer_score_tache(tache: Dict, contexte_meteo: Dict, jour_actuel_index: int) -> Optional[int]:
    """
    Coeur de la logique métier.
    Retourne un score (int) ou None si la tache doit etre ignoree.
    tache contient :
      - intervalle
      - jour_semaine
      - priorite_base
      - priorite_hygiene
      - eviter_pluie, eviter_vent, eviter_gel, eviter_nuit
      - jours_ecoules
    """

    # 1. Filtrage METEO (hard rules)
    if tache["eviter_pluie"] and contexte_meteo["pluie"]:
        return None
    if tache["eviter_vent"] and contexte_meteo["vent"]:
        return None
    if tache["eviter_gel"] and contexte_meteo["gel"]:
        return None
    # eviter_nuit => a implementer si tu veux tenir compte de l'heure

    # 2. Filtrage JOUR SPECIFIQUE via regle.jour_semaine
    jour_cible_str = (tache["jour_semaine"] or "").lower()
    if jour_cible_str in JOURS_MAP:
        jour_cible_idx = JOURS_MAP[jour_cible_str]
        if jour_cible_idx != jour_actuel_index:
            # Mauvais jour => on masque
            return None
        else:
            # Bon jour => priorite absolue
            return 1000

    # 3. Historique : jours_ecoules
    jours_ecoules = tache["jours_ecoules"]
    # Si deja faite aujourd'hui => on ne repropose pas
    if jours_ecoules is not None and jours_ecoules == 0:
        return None

    intervalle = tache["intervalle"]
    priorite_base = tache["priorite_base"] or 0
    priorite_hygiene = tache["priorite_hygiene"] or 0

    # Cas : aucune regle d'intervalle -> tache "occasionnelle"
    if intervalle is None:
        # Si jamais faite -> on la propose avec petit score
        if jours_ecoules is None:
            return priorite_base + priorite_hygiene * 5
        # Si déjà faite au moins une fois et sans intervalle, on ne la pousse pas spécialement
        return None

    # Si jamais faite : gros "retard virtuel"
    jours_effectifs = jours_ecoules if jours_ecoules is not None else 999
    retard = jours_effectifs - intervalle

    if retard < 0:
        # Pas encore due
        return None

    # Score = priorité de base + hygiène + retard
    score = priorite_base + (priorite_hygiene * 10) + (retard * 5)
    return score


# --------- ENDPOINTS SIMPLES ---------

@app.get("/health")
async def health():
    return {"status": "ok"}

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


# --------- AUDIT GLOBAL (INTELLIGENT) ---------

@app.get("/majordome/audit")
async def audit_global():
    """
    Endpoint principal "intelligent" :
      - recupere meteo + jour
      - joint tache + piece + regle + derniere action
      - calcule un score par tache
      - renvoie les top priorites
    """
    # A. Contexte jour + meteo
    jour_index = datetime.now().weekday()  # 0 = lundi

    with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT ville, lat, lon FROM foyer_config WHERE id = 1;")
        cfg = cur.fetchone() or {}

    ville = cfg.get("ville")
    lat = cfg.get("lat")
    lon = cfg.get("lon")

    meteo_raw = _get_meteo_data(lat, lon) if lat is not None and lon is not None else {}
    pluie = (meteo_raw.get("precipitation_sum", [0])[0] or 0)
    vent = (meteo_raw.get("windspeed_10m_max", [0])[0] or 0)
    tmin = (meteo_raw.get("temperature_2m_min", [10])[0] or 10)

    ctx_meteo = {
        "pluie": pluie > 2.0,
        "vent": vent > 50.0,
        "gel": tmin < 2.0,
        "desc": f"Pluie {pluie}mm, Vent {vent}km/h, Tmin {tmin}°C",
    }

    # B. SQL : joint tache + regle + derniere action
    query = """
    WITH DerniereAction AS (
        SELECT id_tache, MAX(horodatage_utc) AS date_derniere
        FROM action
        WHERE statut = 'faite'
        GROUP BY id_tache
    )
    SELECT
        t.id_tache,
        t.nom,
        p.nom AS piece,
        COALESCE(r.intervalle_jours, t.interval_jours) AS intervalle,
        r.jour_semaine,
        COALESCE(r.priorite_base, 50) AS priorite_base,
        t.priorite_hygiene,
        t.eviter_pluie,
        t.eviter_vent,
        t.eviter_neige,
        t.eviter_gel,
        t.eviter_nuit,
        da.date_derniere,
        EXTRACT(DAY FROM (NOW() AT TIME ZONE 'Europe/Paris') - da.date_derniere)::int AS jours_ecoules
    FROM tache t
    JOIN piece p ON p.id_piece = t.id_piece
    LEFT JOIN regle r ON r.id_tache = t.id_tache AND r.active = TRUE
    LEFT JOIN DerniereAction da ON da.id_tache = t.id_tache
    """

    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query)
            rows = cur.fetchall()

        resultats = []
        for row in rows:
            score = _calculer_score_tache(row, ctx_meteo, jour_index)
            if score is None:
                continue

            # Raison (pour trace humaine)
            if row["jour_semaine"]:
                raison = f"Jour specifique: {row['jour_semaine']}"
            elif row["jours_ecoules"] is None:
                raison = "Jamais faite"
            else:
                intervalle = row["intervalle"] or 0
                retard = row["jours_ecoules"] - intervalle
                if retard < 0:
                    raison = "Pas encore due"
                else:
                    raison = f"Retard de {retard} jours"

            resultats.append({
                "tache": row["nom"],
                "piece": row["piece"],
                "score": score,
                "raison": raison,
                "intervalle_jours": row["intervalle"],
                "jours_ecoules": row["jours_ecoules"],
                "jour_semaine": row["jour_semaine"],
                "eviter_pluie": row["eviter_pluie"],
                "eviter_vent": row["eviter_vent"],
                "eviter_gel": row["eviter_gel"],
                "eviter_nuit": row["eviter_nuit"],
            })

        # tri par score décroissant
        resultats.sort(key=lambda x: x["score"], reverse=True)

        jour_nom = list(JOURS_MAP.keys())[jour_index]

        return {
            "meta": {
                "ville": ville,
                "meteo": ctx_meteo["desc"],
                "jour_actuel": jour_nom,
            },
            "top_priorites": resultats[:10],
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur audit_global : {e}")


# --------- ENDPOINT COMPATIBILITE /taches/prioritaires ---------

@app.get("/taches/prioritaires")
async def taches_prioritaires_legacy():
    """
    Endpoint de compatibilite :
    renvoie une liste simple basée sur la logique d'audit.
    """
    data = await audit_global()
    return data["top_priorites"]


# --------- CREATION D UNE TACHE ---------

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
            # 1) Piece
            cur.execute(
                "SELECT id_piece FROM piece WHERE nom = %s;",
                (nouvelle.piece,)
            )
            row_piece = cur.fetchone()
            if not row_piece:
                raise HTTPException(status_code=404, detail="Piece inconnue: " + nouvelle.piece)
            id_piece = row_piece["id_piece"]

            # 2) Valeurs pour regle
            periodicite_regle = nouvelle.periodicite or nouvelle.frequence
            intervalle_regle = (
                nouvelle.regle_intervalle_jours
                if nouvelle.regle_intervalle_jours is not None
                else nouvelle.interval_jours
            )

            # 3) Insertion tache
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

            # 4) Insertion regle
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


# --------- ENREGISTREMENT D UNE ACTION ---------

@app.post("/actions")
async def enregistrer_action(action: Action):
    """
    Enregistre une action dans la table action.
    On cherche la piece et la tache par nom,
    et on renseigne id_membre si on le trouve.
    """
    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            # piece
            cur.execute(
                "SELECT id_piece FROM piece WHERE nom = %s;",
                (action.piece,)
            )
            row_piece = cur.fetchone()
            if not row_piece:
                raise HTTPException(status_code=404, detail="Piece inconnue: " + action.piece)
            id_piece = row_piece["id_piece"]

            # tache
            cur.execute(
                "SELECT id_tache FROM tache WHERE nom = %s AND id_piece = %s;",
                (action.tache, id_piece)
            )
            row_tache = cur.fetchone()
            if not row_tache:
                raise HTTPException(status_code=404, detail="Tache inconnue pour cette piece: " + action.tache)
            id_tache = row_tache["id_tache"]

            # membre (optionnel)
            cur.execute(
                "SELECT id_membre FROM membre WHERE nom_affiche = %s;",
                (action.personne,)
            )
            row_membre = cur.fetchone()
            id_membre = row_membre["id_membre"] if row_membre else None

            # insertion
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