import csv
import sqlite3
from contextlib import closing
from datetime import datetime

DB_PATH = "restaurants.db"
CSV_PATH = "app_restaurants.csv"


def get_conn():
    return sqlite3.connect(DB_PATH)


def _table_cols(cur, table: str) -> set:
    cur.execute(f"PRAGMA table_info({table})")
    return {r[1].lower() for r in cur.fetchall()}


def _ensure_restaurants_has_cols(cur):
    # crea restaurants se non esiste (base)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS restaurants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            city TEXT NOT NULL,
            address TEXT,
            notes TEXT,
            source TEXT NOT NULL,
            lat REAL,
            lon REAL,
            rating REAL,
            last_update TEXT
        )
        """
    )

    cols = _table_cols(cur, "restaurants")

    def add_col_if_missing(col_name: str, col_def: str):
        nonlocal cols
        if col_name.lower() not in cols:
            try:
                cur.execute(f"ALTER TABLE restaurants ADD COLUMN {col_def}")
                cols = _table_cols(cur, "restaurants")
            except Exception:
                pass

    add_col_if_missing("phone", "phone TEXT")
    add_col_if_missing("types", "types TEXT")


def import_app_restaurants():
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        _ensure_restaurants_has_cols(cur)

        print("🔄 Cancello vecchi ristoranti con source = 'app'...")
        cur.execute("DELETE FROM restaurants WHERE source = 'app'")
        conn.commit()

        print(f"📂 Leggo il file CSV: {CSV_PATH}")

        inserted = 0
        now = datetime.utcnow().isoformat()

        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            # normalizza header
            fieldnames = [h.strip().lower() for h in (reader.fieldnames or [])]
            # mappa colonne attese (tollerante)
            def get(row, key):
                return (row.get(key) or "").strip()

            for raw in reader:
                # ricostruisci row case-insensitive
                row = {}
                for k, v in raw.items():
                    if k is None:
                        continue
                    row[k.strip().lower()] = v

                name = get(row, "name")
                city = get(row, "city")
                if not name or not city:
                    continue

                address = get(row, "address") or None
                notes = get(row, "notes") or None

                lat_s = get(row, "lat")
                lon_s = get(row, "lon")
                rating_s = get(row, "rating")

                phone = get(row, "phone") or None
                types = get(row, "types") or None

                try:
                    lat = float(lat_s) if lat_s else None
                except Exception:
                    lat = None
                try:
                    lon = float(lon_s) if lon_s else None
                except Exception:
                    lon = None
                try:
                    rating = float(rating_s) if rating_s else None
                except Exception:
                    rating = None

                last_update = get(row, "last_update") or now

                cur.execute(
                    """
                    INSERT INTO restaurants
                        (name, city, address, notes, source, lat, lon, rating, last_update, phone, types)
                    VALUES (?, ?, ?, ?, 'app', ?, ?, ?, ?, ?, ?)
                    """,
                    (name, city, address, notes, lat, lon, rating, last_update, phone, types),
                )
                inserted += 1

        conn.commit()
        print(f"✅ Import completato. Ristoranti 'app' inseriti: {inserted}")
