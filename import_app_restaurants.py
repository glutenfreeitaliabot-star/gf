import csv
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

DB_PATH = "restaurants.db"
CSV_PATH = "app_restaurants.csv"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    return conn


def _to_float(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def _pick(row: dict, *keys):
    """Prende il primo valore non vuoto tra chiavi alternative."""
    for k in keys:
        if k in row and row[k] is not None:
            val = str(row[k]).strip()
            if val != "":
                return val
    return ""


def _pick_float(row: dict, *keys):
    v = _pick(row, *keys)
    return _to_float(v)


def import_app_restaurants():
    if not os.path.exists(CSV_PATH):
        raise FileNotFoundError(f"CSV non trovato: {CSV_PATH}")

    now = datetime.now(timezone.utc).isoformat()

    with closing(get_conn()) as conn:
        cur = conn.cursor()

        # assicura tabella (se il bot non è partito prima)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS restaurants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                city TEXT NOT NULL,
                address TEXT,
                notes TEXT,
                source TEXT NOT NULL,
                lat TEXT,
                lon TEXT,
                rating REAL,
                last_update TEXT,
                types TEXT,
                phone TEXT
            )
            """
        )
        conn.commit()

        print("🔄 Cancello vecchi ristoranti con source = 'app'...")
        cur.execute("DELETE FROM restaurants WHERE source = 'app'")
        conn.commit()

        print(f"📂 Leggo il file CSV: {CSV_PATH}")
        inserted = 0
        coords_ok = 0

        with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            # normalizzo header: lower-case
            fieldnames = [h.strip() for h in (reader.fieldnames or [])]
            if not fieldnames:
                raise ValueError("CSV senza header (prima riga).")

            # Re-wrap reader per avere chiavi originali ma gestiamo con _pick che prova più varianti
            for row in reader:
                # name/city obbligatori
                name = _pick(row, "name", "Name", "nome", "Nome")
                city = _pick(row, "city", "City", "città", "Città")

                if not name or not city:
                    continue

                address = _pick(row, "address", "Address", "indirizzo", "Indirizzo")
                notes = _pick(row, "notes", "Notes", "note", "Note")
                types = _pick(row, "types", "Types", "type", "Type", "tipologia", "Tipologia")

                phone = _pick(row, "phone", "Phone", "telefono", "Telefono")

                lat = _pick_float(row, "lat", "Lat", "latitude", "Latitude")
                lon = _pick_float(row, "lon", "Lon", "lng", "Lng", "longitude", "Longitude")

                # rating (può essere vuoto)
                rating_raw = _pick(row, "rating", "Rating", "stars", "Stars")
                rating = None
                if rating_raw:
                    try:
                        rating = float(str(rating_raw).strip().replace(",", "."))
                    except Exception:
                        rating = None

                if lat is not None and lon is not None:
                    coords_ok += 1

                # Salvo lat/lon come TEXT (coerente col bot), ma solo se validi
                lat_db = str(lat) if lat is not None else None
                lon_db = str(lon) if lon is not None else None

                cur.execute(
                    """
                    INSERT INTO restaurants
                    (name, city, address, notes, source, lat, lon, rating, last_update, types, phone)
                    VALUES (?, ?, ?, ?, 'app', ?, ?, ?, ?, ?, ?)
                    """,
                    (name, city, address, notes, lat_db, lon_db, rating, now, types, phone),
                )
                inserted += 1

        conn.commit()
        print(f"✅ Import completato. Ristoranti 'app' inseriti: {inserted}")
        print(f"📍 Coordinate valide trovate: {coords_ok}")
