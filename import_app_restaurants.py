import csv
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

DB_PATH = "restaurants.db"
CSV_PATH = "app_restaurants.csv"


def get_conn():
    return sqlite3.connect(DB_PATH)


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
    for k in keys:
        if k in row and row[k] is not None:
            val = str(row[k]).strip()
            if val != "":
                return val
    return ""


def _pick_float(row: dict, *keys):
    return _to_float(_pick(row, *keys))


def import_app_restaurants():
    if not os.path.exists(CSV_PATH):
        raise FileNotFoundError(f"CSV non trovato: {CSV_PATH}")

    now = datetime.now(timezone.utc).isoformat()

    with closing(get_conn()) as conn:
        cur = conn.cursor()

        # === CREATE TABLE (DB nuovi) ===
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
                rating_online_gf REAL,
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

            if not reader.fieldnames:
                raise ValueError("CSV senza header.")

            for row in reader:
                name = _pick(row, "name")
                city = _pick(row, "city")

                if not name or not city:
                    continue

                address = _pick(row, "address")
                notes = _pick(row, "notes")
                types = _pick(row, "types")
                phone = _pick(row, "phone")

                lat = _pick_float(row, "lat")
                lon = _pick_float(row, "lon")

                rating = _to_float(_pick(row, "rating"))
                rating_online_gf = _to_float(_pick(row, "rating_online_gf"))

                if lat is not None and lon is not None:
                    coords_ok += 1

                lat_db = str(lat) if lat is not None else None
                lon_db = str(lon) if lon is not None else None

                cur.execute(
                    """
                    INSERT INTO restaurants (
                        name, city, address, notes, source,
                        lat, lon,
                        rating, rating_online_gf,
                        last_update, types, phone
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name, city, address, notes, "app",
                        lat_db, lon_db,
                        rating, rating_online_gf,
                        now, types, phone,
                    ),
                )

                inserted += 1

        conn.commit()
        print(f"✅ Import completato. Ristoranti inseriti: {inserted}")
        print(f"📍 Coordinate valide trovate: {coords_ok}")
