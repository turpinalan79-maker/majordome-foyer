from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import os
from datetime import datetime
import requests
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()

# --- CONFIG DATABASE ---
DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD")

if not DB_HOST or not DB_PASSWORD:
    raise RuntimeError("Variables d'environnement DB manquantes.")

def get_db():
    return psycopg.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD, sslmode="require"
    )

app = FastAPI(title="Majordome Foyer", version="8.0-prod")

# --------- MODELES ---------

class Action(BaseModel):
    personne: str  # Nom affiché (ex: "Alan")
    piece: str
    tache: str
    commentaire: Optional[str] = None

class NouvelleTache(BaseModel):
    piece: str
    tache: str
    frequence: str
    interval_jours: Optional[int] = None
    priorite_hygiene: int = 3
    periodicite: Optional[str] = None
    regle_intervalle_jours: Optional[int] = None
    jour_semaine: Optional[str] = None
    priorite_base: int = 50
    eviter_pluie: bool = False
    eviter_vent: bool = False
    eviter_neige: bool = False
    eviter_gel: bool = False
    eviter_nuit: bool = False

# --------- OUTILS LOGIQUES ---------

JOURS_MAP = {"lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6}

def _get_meteo_data(lat: float, lon: float) -> Dict:
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_min,windspeed_10m_max,precipitation_sum",
            "forecast_days": 1, "timezone": "Europe/Paris",
        }
        r = requests.get(url, params=params, timeout=3)
        r.raise_for_status()
        return r.json().get("daily", {})
    except:
        return {}

def _calculer_score_tache(tache: Dict, contexte_meteo: Dict, jour_actuel_index: int, heure_actuelle: int, mois_actuel: int) -> Optional[int]:
    # 0. Filtrage par Activation (Sommeil)
    if tache["active"] is False:
        return None

    # 1. Logique NUIT
    est_nuit = (heure_actuelle >= 20 or heure_actuelle < 7)
    if tache["eviter_nuit"] and est_nuit: return None

    # 2. Logique HIVER
    est_hiver = mois_actuel in [12, 1, 2]
    nom_lower = tache["nom"].lower()
    if est_hiver and ("arros" in nom_lower or "tondre" in nom_lower) and tache["eviter_gel"]:
        return None

    # 3. Filtrage METEO
    if tache["eviter_pluie"] and contexte_meteo["pluie"]: return None
    if tache["eviter_vent"] and contexte_meteo["vent"]: return None
    if tache["eviter_gel"] and contexte_meteo["gel"]: return None

    # 4. Filtrage JOUR SPECIFIQUE
    jour_cible_str = (tache["jour_semaine"] or "").lower()
    if jour_cible_str in JOURS_MAP:
        jour_cible_idx = JOURS_MAP[jour_cible_str]
        if jour_cible_idx != jour_actuel_index: return None
        return 1000

    # 5. Historique & Score
    intervalle = tache["intervalle"]
    jours_ecoules = tache["jours_ecoules"]
    priorite_base = tache["priorite_base"] or 0
    priorite_hygiene = tache["priorite_hygiene"] or 0

    # --- CAS TÂCHE PONCTUELLE ---
    if intervalle is None or intervalle == 0:
        if jours_ecoules is None:
            return priorite_base + (priorite_hygiene * 10)
        # Masquée si déjà faite, sauf si active=True (réveillée manuellement)
        return None

    # --- CAS TÂCHE RÉCURRENTE ---
    if jours_ecoules is not None and jours_ecoules == 0: return None
    jours_effectifs = jours_ecoules if jours_ecoules is not None else 999
    retard = jours_effectifs - intervalle
    if retard < 0: return None
    
    return priorite_base + (priorite_hygiene * 10) + (retard * 5)

# --------- ENDPOINTS ---------

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/pieces")
async def liste_pieces():
    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT nom, id_piece FROM piece ORDER BY nom;")
            return cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/taches")
async def lister_taches(
    q: Optional[str] = Query(None, description="Recherche par nom"),
    piece: Optional[str] = Query(None, description="Filtrer par pièce"),
    etat: str = Query("toutes", description="'actives', 'dormantes', ou 'toutes'")
):
    """
    Recherche de tâches (utilisé pour vérifier l'existence avant création).
    """
    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            sql = """
                SELECT t.id_tache, t.nom, p.nom as piece, t.frequence, 
                       t.interval_jours, r.active as est_active
                FROM tache t
                JOIN piece p ON p.id_piece = t.id_piece
                LEFT JOIN regle r ON r.id_tache = t.id_tache
                WHERE TRUE
            """
            args = []
            
            if etat == "dormantes":
                sql += " AND r.active = FALSE"
            elif etat == "actives":
                sql += " AND r.active = TRUE"
            
            if piece:
                sql += " AND p.nom = %s"
                args.append(piece)
            if q:
                sql += " AND t.nom ILIKE %s"
                args.append(f"%{q}%")
            
            sql += " ORDER BY p.nom, t.nom"
            cur.execute(sql, args)
            return cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/taches/{id_tache}/activer")
async def activer_tache(id_tache: int):
    try:
        with get_db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE regle SET active = TRUE WHERE id_tache = %s RETURNING id_regle", (id_tache,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Règle introuvable")
            conn.commit()
        return {"ok": True, "message": "Tâche réactivée."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- AUDIT GLOBAL OU CIBLÉ PAR PIÈCE ---
@app.get("/majordome/audit")
async def audit_global(
    piece: Optional[str] = Query(None, description="Si fourni, ne donne que l'audit de cette pièce")
):
    """
    Le cerveau. 
    Si 'piece' est spécifié (ex: via une photo), on renvoie toutes les urgences de cette pièce.
    Sinon, on renvoie le Top 10 global de la maison.
    """
    now = datetime.now()
    jour_index = now.weekday()
    heure_actuelle = now.hour
    mois_actuel = now.month

    with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT ville, lat, lon FROM foyer_config WHERE id = 1;")
        cfg = cur.fetchone() or {}

    meteo_raw = _get_meteo_data(cfg.get("lat"), cfg.get("lon")) if cfg.get("lat") else {}
    pluie = (meteo_raw.get("precipitation_sum", [0])[0] or 0)
    vent = (meteo_raw.get("windspeed_10m_max", [0])[0] or 0)
    tmin = (meteo_raw.get("temperature_2m_min", [10])[0] or 10)

    ctx_meteo = {
        "pluie": pluie > 2.0, "vent": vent > 50.0, "gel": tmin < 2.0,
        "desc": f"Pluie {pluie}mm, Vent {vent}km/h, Tmin {tmin}°C"
    }

    sql = """
    WITH DerniereAction AS (
        SELECT id_tache, MAX(horodatage_utc) AS date_derniere
        FROM action WHERE statut = 'faite' GROUP BY id_tache
    )
    SELECT 
        t.id_tache, t.nom, p.nom AS piece,
        COALESCE(r.intervalle_jours, t.interval_jours) AS intervalle,
        r.jour_semaine, COALESCE(r.priorite_base, 50) AS priorite_base,
        r.active,
        t.priorite_hygiene, t.eviter_pluie, t.eviter_vent, t.eviter_neige, t.eviter_gel, t.eviter_nuit,
        da.date_derniere,
        EXTRACT(DAY FROM (NOW() AT TIME ZONE 'Europe/Paris') - da.date_derniere)::int AS jours_ecoules
    FROM tache t
    JOIN piece p ON p.id_piece = t.id_piece
    LEFT JOIN regle r ON r.id_tache = t.id_tache
    LEFT JOIN DerniereAction da ON da.id_tache = t.id_tache
    WHERE TRUE
    """
    args = []

    if piece:
        sql += " AND p.nom = %s"
        args.append(piece)

    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()

        resultats = []
        for row in rows:
            score = _calculer_score_tache(row, ctx_meteo, jour_index, heure_actuelle, mois_actuel)
            if score is None: continue

            if row["jour_semaine"]: raison = f"Jour J ({row['jour_semaine']})"
            elif row["active"] and row["intervalle"] is None: raison = "Besoin ponctuel"
            elif row["jours_ecoules"] is None: raison = "Jamais fait"
            else: raison = f"Retard de {row['jours_ecoules'] - (row['intervalle'] or 0)} jours"

            resultats.append({
                "tache": row["nom"], "piece": row["piece"], "score": score, "raison": raison,
                "type": "Ponctuelle" if row["intervalle"] is None else "Récurrente"
            })

        resultats.sort(key=lambda x: x["score"], reverse=True)
        limite = len(resultats) if piece else 10

        return {
            "meta": {
                "ville": cfg.get("ville"), "meteo": ctx_meteo["desc"],
                "contexte": f"Audit de : {piece}" if piece else "Audit Global",
                "heure": f"{heure_actuelle}h"
            },
            "priorites": resultats[:limite]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/taches")
async def creer_tache(nouvelle: NouvelleTache):
    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            # 1. Pièce
            cur.execute("SELECT id_piece FROM piece WHERE nom = %s", (nouvelle.piece,))
            p = cur.fetchone()
            if not p: raise HTTPException(status_code=404, detail="Pièce inconnue")
            id_piece = p["id_piece"]

            # 2. Check Doublon
            cur.execute("SELECT id_tache FROM tache WHERE id_piece = %s AND LOWER(nom) = LOWER(%s)", (id_piece, nouvelle.tache))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail=f"La tâche existe déjà.")

            # 3. Ponctuel vs Récurrent
            if nouvelle.frequence.lower() == "ponctuelle":
                final_interval = None
                periodicite_regle = "ponctuelle"
            else:
                final_interval = nouvelle.interval_jours
                periodicite_regle = nouvelle.periodicite or nouvelle.frequence

            # 4. Insert Tache
            cur.execute("""
                INSERT INTO tache (nom, id_piece, frequence, interval_jours, priorite_hygiene,
                                   eviter_pluie, eviter_vent, eviter_neige, eviter_gel, eviter_nuit)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id_tache
            """, (nouvelle.tache, id_piece, nouvelle.frequence, final_interval, nouvelle.priorite_hygiene,
                  nouvelle.eviter_pluie, nouvelle.eviter_vent, nouvelle.eviter_neige, nouvelle.eviter_gel, nouvelle.eviter_nuit))
            id_tache = cur.fetchone()["id_tache"]

            # 5. Insert Règle
            cur.execute("""
                INSERT INTO regle (id_piece, id_tache, periodicite, intervalle_jours, jour_semaine, priorite_base, active)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            """, (id_piece, id_tache, periodicite_regle, final_interval, nouvelle.jour_semaine, nouvelle.priorite_base))
            
            conn.commit()
        return {"ok": True, "message": f"Tâche '{nouvelle.tache}' créée."}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/taches/{id_tache}")
async def supprimer_tache(id_tache: int):
    try:
        with get_db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM tache WHERE id_tache = %s RETURNING nom", (id_tache,))
            row = cur.fetchone()
            if not row: raise HTTPException(status_code=404, detail="Tâche introuvable")
            conn.commit()
        return {"ok": True, "message": f"Tâche '{row[0]}' supprimée."}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/actions")
async def enregistrer_action(action: Action):
    """
    CORRECTION MAJEURE : Recherche du membre ID.
    """
    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            # 1. Trouver la pièce
            cur.execute("SELECT id_piece FROM piece WHERE nom = %s", (action.piece,))
            p = cur.fetchone()
            if not p: return {"error": "Pièce inconnue"}
            
            # 2. Trouver la tâche
            cur.execute("SELECT id_tache, interval_jours FROM tache WHERE nom = %s AND id_piece = %s", (action.tache, p['id_piece']))
            t = cur.fetchone()
            if not t: return {"error": "Tâche inconnue"}

            # 3. TROUVER LE MEMBRE (Le correctif demandé)
            cur.execute("SELECT id_membre FROM membre WHERE nom_affiche = %s", (action.personne,))
            m = cur.fetchone()
            id_membre = m['id_membre'] if m else None

            # 4. Insérer l'action
            cur.execute("""
                INSERT INTO action (horodatage_utc, id_membre, id_piece, id_tache, statut, commentaire, origine)
                VALUES (NOW(), %s, %s, %s, 'faite', %s, 'api_majordome')
            """, (id_membre, p['id_piece'], t['id_tache'], action.commentaire))

            # 5. Si Ponctuelle -> Désactiver
            if t['interval_jours'] is None or t['interval_jours'] == 0:
                cur.execute("UPDATE regle SET active = FALSE WHERE id_tache = %s", (t['id_tache'],))

            conn.commit()
        return {"ok": True}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- COMPATIBILITE ---
@app.get("/taches/prioritaires")
async def taches_prioritaires_legacy():
    """Redirection pour compatibilité si l'ancien endpoint est appelé."""
    data = await audit_global(None)
    return data["priorites"]