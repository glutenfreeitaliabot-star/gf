import csv
import hashlib
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "restaurants.db")
CSV_PATH = os.getenv("CSV_PATH", "app_restaurants.csv")
SQLITE_TIMEOUT_SECONDS = float(os.getenv("SQLITE_TIMEOUT_SECONDS", "30"))
CSV_SOURCE = "app"


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def _table_columns(cur: sqlite3.Cursor, table: str) -> dict:
    cur.execute(f"PRAGMA table_info({table})")
    return {row[1]: row for row in cur.fetchall()}


def _safe_add_column(cur: sqlite3.Cursor, table: str, column_sql: str) -> None:
    column_name = column_sql.split()[0]
    if column_name not in _table_columns(cur, table):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column_sql}")


def _ensure_restaurants_schema(cur: sqlite3.Cursor) -> None:
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
            phone TEXT,
            website TEXT,
            google_maps_url TEXT,
            place_id TEXT,
            source_uid TEXT,
            is_active INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    _safe_add_column(cur, "restaurants", "rating_online_gf REAL")
    _safe_add_column(cur, "restaurants", "types TEXT")
    _safe_add_column(cur, "restaurants", "phone TEXT")
    _safe_add_column(cur, "restaurants", "website TEXT")
    _safe_add_column(cur, "restaurants", "google_maps_url TEXT")
    _safe_add_column(cur, "restaurants", "place_id TEXT")
    _safe_add_column(cur, "restaurants", "source_uid TEXT")
    _safe_add_column(cur, "restaurants", "is_active INTEGER NOT NULL DEFAULT 1")

    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_restaurants_source_uid ON restaurants(source_uid) WHERE source_uid IS NOT NULL"
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_restaurants_source ON restaurants(source)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_restaurants_is_active ON restaurants(is_active)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_restaurants_place_id ON restaurants(place_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_restaurants_google_maps_url ON restaurants(google_maps_url)")


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


def _normalized_piece(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _build_source_uid(row: dict) -> str:
    place_id = _pick(row, "place_id")
    if place_id:
        return f"{CSV_SOURCE}:place_id:{place_id}"

    google_maps_url = _pick(row, "google_maps_url")
    if google_maps_url:
        return f"{CSV_SOURCE}:gmaps:{google_maps_url}"

    fingerprint = "|".join(
        [
            CSV_SOURCE,
            _normalized_piece(_pick(row, "name")),
            _normalized_piece(_pick(row, "city")),
            _normalized_piece(_pick(row, "address")),
            _normalized_piece(_pick(row, "phone")),
        ]
    )
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()
    return f"{CSV_SOURCE}:hash:{digest}"


def _find_existing_restaurant(cur: sqlite3.Cursor, row: dict, source_uid: str) -> Optional[sqlite3.Row]:
    cur.execute("SELECT id, COALESCE(is_active, 1) AS is_active FROM restaurants WHERE source_uid = ? LIMIT 1", (source_uid,))
    existing = cur.fetchone()
    if existing:
        return existing

    place_id = _pick(row, "place_id")
    if place_id:
        cur.execute(
            "SELECT id, COALESCE(is_active, 1) AS is_active FROM restaurants WHERE source = ? AND place_id = ? LIMIT 1",
            (CSV_SOURCE, place_id),
        )
        existing = cur.fetchone()
        if existing:
            return existing

    google_maps_url = _pick(row, "google_maps_url")
    if google_maps_url:
        cur.execute(
            "SELECT id, COALESCE(is_active, 1) AS is_active FROM restaurants WHERE source = ? AND google_maps_url = ? LIMIT 1",
            (CSV_SOURCE, google_maps_url),
        )
        existing = cur.fetchone()
        if existing:
            return existing

    name = _pick(row, "name")
    city = _pick(row, "city")
    address = _pick(row, "address")
    cur.execute(
        """
        SELECT id, COALESCE(is_active, 1) AS is_active
        FROM restaurants
        WHERE source = ?
          AND lower(name) = lower(?)
          AND lower(city) = lower(?)
          AND lower(COALESCE(address, '')) = lower(?)
        LIMIT 1
        """,
        (CSV_SOURCE, name, city, address),
    )
    return cur.fetchone()


def import_app_restaurants():
    if not os.path.exists(CSV_PATH):
        raise FileNotFoundError(f"CSV non trovato: {CSV_PATH}")

    now = datetime.now(timezone.utc).isoformat()

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        _ensure_restaurants_schema(cur)
        conn.commit()

        print(f"📂 Leggo il file CSV: {CSV_PATH}")
        inserted = 0
        updated = 0
        reactivated = 0
        skipped = 0
        coords_ok = 0

        cur.execute("DROP TABLE IF EXISTS tmp_imported_source_uids")
        cur.execute("CREATE TEMP TABLE tmp_imported_source_uids (source_uid TEXT PRIMARY KEY)")

        with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                raise ValueError("CSV senza header.")

            for row in reader:
                name = _pick(row, "name")
                city = _pick(row, "city")
                if not name or not city:
                    skipped += 1
                    continue

                address = _pick(row, "address")
                notes = _pick(row, "notes")
                types = _pick(row, "types")
                phone = _pick(row, "phone")
                website = _pick(row, "website")
                google_maps_url = _pick(row, "google_maps_url")
                place_id = _pick(row, "place_id")
                csv_last_update = _pick(row, "last_update") or now

                lat = _pick_float(row, "lat")
                lon = _pick_float(row, "lon")
                rating = _to_float(_pick(row, "rating"))
                rating_online_gf = _to_float(_pick(row, "rating_online_gf"))
                source_uid = _build_source_uid(row)

                if lat is not None and lon is not None:
                    coords_ok += 1

                lat_db = str(lat) if lat is not None else None
                lon_db = str(lon) if lon is not None else None

                cur.execute("INSERT OR IGNORE INTO tmp_imported_source_uids(source_uid) VALUES (?)", (source_uid,))
                existing = _find_existing_restaurant(cur, row, source_uid)

                payload = (
                    name,
                    city,
                    address,
                    notes,
                    CSV_SOURCE,
                    lat_db,
                    lon_db,
                    rating,
                    rating_online_gf,
                    csv_last_update,
                    types,
                    phone,
                    website,
                    google_maps_url,
                    place_id,
                    source_uid,
                )

                if existing:
                    cur.execute(
                        """
                        UPDATE restaurants
                        SET name = ?,
                            city = ?,
                            address = ?,
                            notes = ?,
                            source = ?,
                            lat = ?,
                            lon = ?,
                            rating = ?,
                            rating_online_gf = ?,
                            last_update = ?,
                            types = ?,
                            phone = ?,
                            website = ?,
                            google_maps_url = ?,
                            place_id = ?,
                            source_uid = ?,
                            is_active = 1
                        WHERE id = ?
                        """,
                        payload + (existing["id"],),
                    )
                    updated += 1
                    if int(existing["is_active"] or 0) == 0:
                        reactivated += 1
                else:
                    cur.execute(
                        """
                        INSERT INTO restaurants (
                            name,
                            city,
                            address,
                            notes,
                            source,
                            lat,
                            lon,
                            rating,
                            rating_online_gf,
                            last_update,
                            types,
                            phone,
                            website,
                            google_maps_url,
                            place_id,
                            source_uid,
                            is_active
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        payload,
                    )
                    inserted += 1

        cur.execute(
            """
            UPDATE restaurants
            SET is_active = 0,
                last_update = ?
            WHERE source = ?
              AND (
                    source_uid IS NULL
                    OR source_uid NOT IN (SELECT source_uid FROM tmp_imported_source_uids)
                  )
            """,
            (now, CSV_SOURCE),
        )
        deactivated = cur.rowcount

        conn.commit()
        print(f"✅ Import completato. Inseriti: {inserted} • Aggiornati: {updated} • Riattivati: {reactivated}")
        print(f"🧹 Righe disattivate perché assenti nel CSV: {deactivated}")
        print(f"📍 Coordinate valide trovate: {coords_ok} • Righe saltate: {skipped}")


if __name__ == "__main__":
    import_app_restaurants()
