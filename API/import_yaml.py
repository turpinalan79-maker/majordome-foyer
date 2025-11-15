import os
import yaml
from psycopg.rows import dict_row
from config_db import get_conn

YAML_FILE = "descriptif_foyer.yaml"

MAP_INTERVAL = {
    "quotidienne": 1,
    "plurihebdomadaire": 3,
    "hebdomadaire": 7,
    "bimensuelle": 14,
    "mensuelle": 30,
    "saisonniere": 90,
    "occasionnelle": None,
}

KW_NIGHT_AVOID = ("vitre", "fenetr", "fenêtre", "jardin", "terrasse", "garage")
KW_TRASH_ALLOW = ("poubell", "ordure", "dechet", "déchet", "recycl")

def get_interval(frequence, interval_jours_yaml):
    if interval_jours_yaml is not None:
        return interval_jours_yaml
    if not frequence:
        return None
    return MAP_INTERVAL.get(frequence.lower(), None)

def guess_eviter_nuit(piece_nom: str, tache_nom: str) -> bool:
    p = (piece_nom or "").lower()
    t = (tache_nom or "").lower()
    if any(k in t for k in KW_TRASH_ALLOW):
        return False
    if "extérieur" in p or "exter" in p or "garage" in p or "jardin" in p or "terrasse" in p:
        return True
    if any(k in t for k in KW_NIGHT_AVOID):
        return True
    return False

def main():
    if not os.path.exists(YAML_FILE):
        raise FileNotFoundError(f"Fichier introuvable: {YAML_FILE}")

    with open(YAML_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    membres = data.get("membres", [])
    pieces  = data.get("pieces", [])

    with get_conn() as conn:
        conn.row_factory = dict_row
        cur = conn.cursor()

        # Schéma
        cur.execute("""
CREATE TABLE IF NOT EXISTS membre (
  id_membre BIGSERIAL PRIMARY KEY,
  nom_affiche TEXT NOT NULL UNIQUE,
  actif BOOLEAN NOT NULL DEFAULT TRUE
);
""")
        cur.execute("""
CREATE TABLE IF NOT EXISTS piece (
  id_piece BIGSERIAL PRIMARY KEY,
  nom TEXT NOT NULL UNIQUE,
  superficie_m2 INT NULL,
  etage TEXT NULL,
  exposition TEXT NULL,
  type_sol TEXT NULL
);
""")
        cur.execute("""
CREATE TABLE IF NOT EXISTS tache (
  id_tache BIGSERIAL PRIMARY KEY,
  nom TEXT NOT NULL,
  id_piece BIGINT NOT NULL REFERENCES piece(id_piece) ON DELETE CASCADE,
  frequence TEXT NOT NULL,
  interval_jours INT NULL,
  priorite_hygiene INT NOT NULL,
  eviter_pluie boolean NOT NULL DEFAULT false,
  eviter_vent  boolean NOT NULL DEFAULT false,
  eviter_neige boolean NOT NULL DEFAULT false,
  eviter_gel   boolean NOT NULL DEFAULT false,
  eviter_nuit  boolean NOT NULL DEFAULT false
);
""")
        cur.execute("""
CREATE UNIQUE INDEX IF NOT EXISTS uq_tache_piece_nom
ON tache(id_piece, nom);
""")
        cur.execute("""
CREATE TABLE IF NOT EXISTS foyer_config (
  id int PRIMARY KEY DEFAULT 1,
  ville text,
  lat double precision,
  lon double precision
);
""")
        cur.execute("""
CREATE TABLE IF NOT EXISTS action (
  id_action BIGSERIAL PRIMARY KEY,
  id_membre BIGINT NULL,
  id_piece  BIGINT NOT NULL REFERENCES piece(id_piece) ON DELETE CASCADE,
  id_tache  BIGINT NOT NULL REFERENCES tache(id_tache) ON DELETE CASCADE,
  horodatage_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  statut TEXT NOT NULL DEFAULT 'faite',
  commentaire TEXT,
  origine TEXT
);
""")
        cur.execute("""
CREATE TABLE IF NOT EXISTS alerte_regle (
  code text PRIMARY KEY,
  actif boolean NOT NULL DEFAULT true,
  seuil_num numeric NULL,
  details jsonb NULL
);
""")
        cur.execute("""
CREATE TABLE IF NOT EXISTS alerte_notif (
  id bigserial PRIMARY KEY,
  code text NOT NULL,
  titre text NOT NULL,
  message text NOT NULL,
  niveau text NULL,
  horodatage timestamptz NOT NULL DEFAULT now()
);
""")

        # Membres
        for m in membres:
            nom = m.get("nom")
            if not nom: continue
            cur.execute(
                "INSERT INTO membre (nom_affiche, actif) VALUES (%s, TRUE) ON CONFLICT (nom_affiche) DO NOTHING;",
                (nom,)
            )

        # Pièces + zones -> tâches
        for p in pieces:
            nom_piece   = p.get("nom")
            if not nom_piece: continue
            superficie  = p.get("superficie_m2")
            etage       = p.get("etage")
            exposition  = p.get("exposition")
            type_sol    = p.get("type_sol")

            row = cur.execute("""
INSERT INTO piece (nom, superficie_m2, etage, exposition, type_sol)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (nom) DO UPDATE SET
  superficie_m2 = EXCLUDED.superficie_m2,
  etage         = EXCLUDED.etage,
  exposition    = EXCLUDED.exposition,
  type_sol      = EXCLUDED.type_sol
RETURNING id_piece;
""", (nom_piece, superficie, etage, exposition, type_sol)).fetchone()
            id_piece = row["id_piece"]

            for z in p.get("zones", []):
                nom_zone  = z.get("nom")
                if not nom_zone: continue
                frequence = (z.get("frequence") or "occasionnelle").lower()
                priorite  = int(z.get("priorite_hygiene", 3))
                interval  = z.get("interval_jours")
                if interval is None:
                    interval = get_interval(frequence, None)

                ev_pluie  = bool(z.get("eviter_pluie", False))
                ev_vent   = bool(z.get("eviter_vent", False))
                ev_neige  = bool(z.get("eviter_neige", False))
                ev_gel    = bool(z.get("eviter_gel", False))
                ev_nuit   = bool(z.get("eviter_nuit", guess_eviter_nuit(nom_piece, nom_zone)))

                cur.execute("""
INSERT INTO tache (nom, id_piece, frequence, interval_jours, priorite_hygiene,
                   eviter_pluie, eviter_vent, eviter_neige, eviter_gel, eviter_nuit)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id_piece, nom) DO UPDATE SET
  frequence = EXCLUDED.frequence,
  interval_jours = EXCLUDED.interval_jours,
  priorite_hygiene = EXCLUDED.priorite_hygiene,
  eviter_pluie = EXCLUDED.eviter_pluie,
  eviter_vent  = EXCLUDED.eviter_vent,
  eviter_neige = EXCLUDED.eviter_neige,
  eviter_gel   = EXCLUDED.eviter_gel,
  eviter_nuit  = EXCLUDED.eviter_nuit;
""", (nom_zone, id_piece, frequence, interval, priorite,
    ev_pluie, ev_vent, ev_neige, ev_gel, ev_nuit))

        # Règles d'alertes par défaut
        cur.execute("INSERT INTO alerte_regle (code, seuil_num, details) VALUES ('gel', 0, '{}'::jsonb) ON CONFLICT (code) DO NOTHING;")
        cur.execute("INSERT INTO alerte_regle (code, seuil_num, details) VALUES ('vent', 70, '{}'::jsonb) ON CONFLICT (code) DO NOTHING;")
        cur.execute("""
            INSERT INTO alerte_regle (code, seuil_num, details)
            VALUES ('lavage_voiture', NULL, '{"frequence_jours":14}'::jsonb)
            ON CONFLICT (code) DO NOTHING;
        """)

        conn.commit()

    print("✅ Import terminé : membres, pièces et tâches à jour (avec eviter_nuit).")

if __name__ == "__main__":
    main()
