# serveur_majordome.py

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, Dict
import os
from datetime import datetime
from zoneinfo import ZoneInfo
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

PARIS_TZ = ZoneInfo("Europe/Paris")
UTC_TZ = ZoneInfo("UTC")

def now_paris() -> datetime:
    return datetime.now(PARIS_TZ)

def get_db():
    return psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        sslmode="require",
    )

app = FastAPI(title="Majordome Foyer", version="9.1-paris-time")

# --------- MODELES ---------

class Action(BaseModel):
    personne: str
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

# --------- LOGIQUE METIER ---------

JOURS_MAP = {"lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6}
JOURS_INVERSE = {v: k for k, v in JOURS_MAP.items()}

def _get_meteo_data(lat: float, lon: float) -> Dict:
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_min,windspeed_10m_max,precipitation_sum",
            "forecast_days": 1,
            "timezone": "Europe/Paris",
        }
        r = requests.get(url, params=params, timeout=3)
        r.raise_for_status()
        return r.json().get("daily", {})
    except Exception:
        return {}

def _analyser_tache(tache: Dict, contexte_meteo: Dict, jour_actuel_index: int, heure_actuelle: int, mois_actuel: int) -> Dict:
    res = {"visible": False, "score": 0, "raison": "", "echeance": "Inconnue"}

    # 0. Activation
    # Si aucune regle => active peut etre NULL => on considere active
    if tache.get("active") is False:
        res["raison"] = "Tache en sommeil (deja faite / desactivee)."
        res["echeance"] = "Sur demande"
        return res

    # 1. NUIT
    est_nuit = (heure_actuelle >= 20 or heure_actuelle < 7)
    if tache.get("eviter_nuit") and est_nuit:
        res["raison"] = "Reporte : il fait nuit."
        res["echeance"] = "Demain matin"
        return res

    # 2. HIVER
    est_hiver = mois_actuel in [10, 11, 12, 1, 2, 3]
    nom_lower = (tache.get("nom") or "").lower()
    if est_hiver and ("arros" in nom_lower or "tondre" in nom_lower) and tache.get("eviter_gel"):
        res["raison"] = "Reporte : saison hivernale / automnale."
        res["echeance"] = "Printemps"
        return res

    # 3. METEO
    if tache.get("eviter_pluie") and contexte_meteo.get("pluie"):
        res["raison"] = "Reporte : il pleut."
        res["echeance"] = "Des qu'il fait beau"
        return res
    if tache.get("eviter_vent") and contexte_meteo.get("vent"):
        res["raison"] = "Reporte : trop de vent."
        return res
    if tache.get("eviter_gel") and contexte_meteo.get("gel"):
        res["raison"] = "Reporte : risque de gel."
        return res

    # 4. JOUR SPECIFIQUE
    jour_cible_str = (tache.get("jour_semaine") or "").lower()
    if jour_cible_str in JOURS_MAP:
        jour_cible_idx = JOURS_MAP[jour_cible_str]
        if jour_cible_idx != jour_actuel_index:
            res["raison"] = f"Planifie pour {jour_cible_str.capitalize()}."
            delta = (jour_cible_idx - jour_actuel_index) % 7
            if delta == 0:
                delta = 7
            res["echeance"] = f"Dans {delta} jours ({jour_cible_str})"
            return res
        res["visible"] = True
        res["score"] = 1000
        res["raison"] = f"C'est le jour J ({jour_cible_str}) !"
        res["echeance"] = "Aujourd'hui"
        return res

    # 5. CALCUL HISTORIQUE & RETARD
    intervalle = tache.get("intervalle")
    jours_ecoules = tache.get("jours_ecoules")
    priorite_base = tache.get("priorite_base") or 0
    priorite_hygiene = tache.get("priorite_hygiene") or 0

    # Cas Ponctuel
    if intervalle is None or intervalle == 0:
        if jours_ecoules is None:
            res["visible"] = True
            res["score"] = priorite_base + (priorite_hygiene * 10)
            res["raison"] = "Jamais fait (ponctuel)."
            res["echeance"] = "Des que possible"
        else:
            res["visible"] = True
            res["score"] = 900
            res["raison"] = "Ponctuelle reactivee."
            res["echeance"] = "Des que possible"
        return res

    # Cas Recurrent
    if jours_ecoules is not None and jours_ecoules == 0:
        res["raison"] = "Deja fait aujourd'hui."
        res["echeance"] = f"Dans {intervalle} jours"
        return res

    jours_effectifs = jours_ecoules if jours_ecoules is not None else 999
    retard = jours_effectifs - intervalle

    if retard < 0:
        res["raison"] = f"A jour (fait il y a {jours_ecoules}j)."
        res["echeance"] = f"Dans {abs(retard)} jours"
        return res

    # Due
    res["visible"] = True
    res["score"] = priorite_base + (priorite_hygiene * 10) + (retard * 5)
    res["raison"] = f"En retard de {retard} jours."
    res["echeance"] = "Maintenant"
    return res

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

# --- ENDPOINT D'EXPLICATION ---
@app.get("/taches/infos")
async def infos_tache(q: str = Query(..., description="Nom de la tache a analyser")):
    now = now_paris()
    jour_index = now.weekday()
    heure = now.hour
    mois = now.month

    # Meteo
    with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT ville, lat, lon FROM foyer_config WHERE id = 1;")
        cfg = cur.fetchone() or {}

    meteo_raw = _get_meteo_data(cfg.get("lat"), cfg.get("lon")) if cfg.get("lat") else {}
    pluie = (meteo_raw.get("precipitation_sum", [0])[0] or 0)
    vent = (meteo_raw.get("windspeed_10m_max", [0])[0] or 0)
    tmin = (meteo_raw.get("temperature_2m_min", [10])[0] or 10)
    ctx_meteo = {"pluie": pluie > 2.0, "vent": vent > 50.0, "gel": tmin < 2.0}

    # IMPORTANT :
    # - On force 1 seule regle par tache (LATERAL)
    # - On calcule jours_ecoules par "jour calendrier" en heure de Paris
    # - On suppose que action.horodatage_utc est stocke en UTC dans une colonne timestamp (sans tz)
    sql = """
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
        COALESCE(r.active, TRUE) AS active,
        t.priorite_hygiene,
        t.eviter_pluie, t.eviter_vent, t.eviter_neige, t.eviter_gel, t.eviter_nuit,
        da.date_derniere,
        (
          DATE(NOW() AT TIME ZONE 'Europe/Paris')
          - DATE(((da.date_derniere AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Paris'))
        )::int AS jours_ecoules
    FROM tache t
    JOIN piece p ON p.id_piece = t.id_piece
    LEFT JOIN LATERAL (
        SELECT r.*
        FROM regle r
        WHERE r.id_tache = t.id_tache
        ORDER BY r.active DESC, r.id_regle DESC
        LIMIT 1
    ) r ON TRUE
    LEFT JOIN DerniereAction da ON da.id_tache = t.id_tache
    WHERE t.nom ILIKE %s
    """

    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (f"%{q.strip()}%",))
            rows = cur.fetchall()

        if not rows:
            return {"found": False, "message": "Aucune tache trouvee avec ce nom."}

        resultats = []
        for row in rows:
            analyse = _analyser_tache(row, ctx_meteo, jour_index, heure, mois)
            resultats.append({
                "tache": row["nom"],
                "piece": row["piece"],
                "statut": "Prioritaire" if analyse["visible"] else "En attente",
                "explication": analyse["raison"],
                "prevision": analyse["echeance"],
                "derniere_fois": f"Il y a {row['jours_ecoules']} jours" if row["jours_ecoules"] is not None else "Jamais",
            })

        return {"found": True, "resultats": resultats}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/taches")
async def lister_taches(
    q: Optional[str] = Query(None),
    piece: Optional[str] = Query(None),
    etat: str = Query("toutes"),
):
    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            sql = """
                SELECT t.id_tache, t.nom, p.nom as piece, t.frequence,
                       t.interval_jours, COALESCE(r.active, TRUE) as est_active
                FROM tache t
                JOIN piece p ON p.id_piece = t.id_piece
                LEFT JOIN LATERAL (
                    SELECT r.*
                    FROM regle r
                    WHERE r.id_tache = t.id_tache
                    ORDER BY r.active DESC, r.id_regle DESC
                    LIMIT 1
                ) r ON TRUE
                WHERE TRUE
            """
            args = []
            if etat == "dormantes":
                sql += " AND COALESCE(r.active, TRUE) = FALSE"
            elif etat == "actives":
                sql += " AND COALESCE(r.active, TRUE) = TRUE"
            if piece:
                sql += " AND p.nom = %s"
                args.append(piece.strip())
            if q:
                sql += " AND t.nom ILIKE %s"
                args.append(f"%{q.strip()}%")
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
            conn.commit()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/majordome/audit")
async def audit_global(piece: Optional[str] = Query(None)):
    now = now_paris()
    jour_index = now.weekday()
    heure = now.hour
    mois = now.month

    with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT ville, lat, lon FROM foyer_config WHERE id = 1;")
        cfg = cur.fetchone() or {}

    meteo_raw = _get_meteo_data(cfg.get("lat"), cfg.get("lon")) if cfg.get("lat") else {}
    pluie = (meteo_raw.get("precipitation_sum", [0])[0] or 0)
    vent = (meteo_raw.get("windspeed_10m_max", [0])[0] or 0)
    tmin = (meteo_raw.get("temperature_2m_min", [10])[0] or 10)
    ctx_meteo = {"pluie": pluie > 2.0, "vent": vent > 50.0, "gel": tmin < 2.0, "desc": f"Pluie {pluie}mm"}

    sql = """
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
        COALESCE(r.active, TRUE) AS active,
        t.priorite_hygiene,
        t.eviter_pluie, t.eviter_vent, t.eviter_neige, t.eviter_gel, t.eviter_nuit,
        da.date_derniere,
        (
          DATE(NOW() AT TIME ZONE 'Europe/Paris')
          - DATE(((da.date_derniere AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Paris'))
        )::int AS jours_ecoules
    FROM tache t
    JOIN piece p ON p.id_piece = t.id_piece
    LEFT JOIN LATERAL (
        SELECT r.*
        FROM regle r
        WHERE r.id_tache = t.id_tache
        ORDER BY r.active DESC, r.id_regle DESC
        LIMIT 1
    ) r ON TRUE
    LEFT JOIN DerniereAction da ON da.id_tache = t.id_tache
    WHERE TRUE
    """
    args = []
    if piece:
        sql += " AND p.nom = %s"
        args.append(piece.strip())

    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()

        resultats = []
        for row in rows:
            analyse = _analyser_tache(row, ctx_meteo, jour_index, heure, mois)
            if analyse["visible"]:
                resultats.append({
                    "tache": row["nom"],
                    "piece": row["piece"],
                    "score": analyse["score"],
                    "raison": analyse["raison"],
                    "type": "Ponctuelle" if row["intervalle"] is None else "Recurrente",
                })

        resultats.sort(key=lambda x: x["score"], reverse=True)
        limite = len(resultats) if piece else 20

        return {
            "meta": {
                "ville": cfg.get("ville"),
                "meteo": ctx_meteo["desc"],
                "contexte": f"Audit: {piece}" if piece else "Audit Global",
                "heure_paris": now.strftime("%Y-%m-%d %H:%M:%S"),
            },
            "priorites": resultats[:limite],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/taches")
async def creer_tache(nouvelle: NouvelleTache):
    try:
        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id_piece FROM piece WHERE trim(nom) ILIKE %s", (nouvelle.piece.strip(),))
            p = cur.fetchone()
            if not p:
                raise HTTPException(status_code=404, detail="Piece inconnue")
            id_piece = p["id_piece"]

            # Check doublon (robuste)
            cur.execute(
                "SELECT id_tache FROM tache WHERE id_piece = %s AND trim(lower(nom)) = trim(lower(%s))",
                (id_piece, nouvelle.tache),
            )
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="Existe deja.")

            if nouvelle.frequence.lower() == "ponctuelle":
                final_int = None
                regle_per = "ponctuelle"
            else:
                final_int = nouvelle.interval_jours
                regle_per = nouvelle.periodicite or nouvelle.frequence

            cur.execute(
                """
                INSERT INTO tache (nom, id_piece, frequence, interval_jours, priorite_hygiene,
                                   eviter_pluie, eviter_vent, eviter_neige, eviter_gel, eviter_nuit)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id_tache
                """,
                (
                    nouvelle.tache.strip(),
                    id_piece,
                    nouvelle.frequence,
                    final_int,
                    nouvelle.priorite_hygiene,
                    nouvelle.eviter_pluie,
                    nouvelle.eviter_vent,
                    nouvelle.eviter_neige,
                    nouvelle.eviter_gel,
                    nouvelle.eviter_nuit,
                ),
            )
            id_tache = cur.fetchone()["id_tache"]

            cur.execute(
                """
                INSERT INTO regle (id_piece, id_tache, periodicite, intervalle_jours, jour_semaine, priorite_base, active)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                """,
                (id_piece, id_tache, regle_per, final_int, nouvelle.jour_semaine, nouvelle.priorite_base),
            )
            conn.commit()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/taches/{id_tache}")
async def supprimer_tache(id_tache: int):
    try:
        with get_db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM tache WHERE id_tache = %s RETURNING nom", (id_tache,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Inconnue")
            conn.commit()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/actions")
async def enregistrer_action(action: Action):
    try:
        # Normalisation (evite les espaces/casse)
        piece_nom = (action.piece or "").strip()
        tache_nom = (action.tache or "").strip()
        personne_nom = (action.personne or "").strip()

        with get_db() as conn, conn.cursor(row_factory=dict_row) as cur:
            # Piece (robuste)
            cur.execute("SELECT id_piece FROM piece WHERE trim(nom) ILIKE %s", (piece_nom,))
            p = cur.fetchone()
            if not p:
                return {"error": "Piece inconnue"}

            # Tache (robuste)
            cur.execute(
                """
                SELECT id_tache, interval_jours
                FROM tache
                WHERE id_piece = %s
                  AND trim(nom) ILIKE %s
                ORDER BY id_tache DESC
                LIMIT 1
                """,
                (p["id_piece"], tache_nom),
            )
            t = cur.fetchone()
            if not t:
                return {"error": "Tache inconnue (verifie le libelle exact dans /taches)"}

            # Membre
            cur.execute("SELECT id_membre FROM membre WHERE trim(nom_affiche) ILIKE %s", (personne_nom,))
            m = cur.fetchone()
            id_mbr = m["id_membre"] if m else None

            # IMPORTANT : horodatage_utc stocke en UTC
            # On force l'insertion en UTC (colonne timestamp sans tz en general).
            cur.execute(
                """
                INSERT INTO action (horodatage_utc, id_membre, id_piece, id_tache, statut, commentaire, origine)
                VALUES ((NOW() AT TIME ZONE 'UTC'), %s, %s, %s, 'faite', %s, 'api_majordome')
                """,
                (id_mbr, p["id_piece"], t["id_tache"], action.commentaire),
            )

            # Ponctuelle => desactivation regle
            if t["interval_jours"] is None or t["interval_jours"] == 0:
                cur.execute("UPDATE regle SET active = FALSE WHERE id_tache = %s", (t["id_tache"],))

            conn.commit()

        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
