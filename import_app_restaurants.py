import csv
import sqlite3
from contextlib import closing

DB_PATH = "restaurants.db"
CSV_PATH = "app_restaurants.csv"


def safe_float(value, field_name, row):
    """
    Converte automaticamente numeri con virgola → punto.
    Esempio: "44,8324543" -> 44.8324543

    Se non riesce, logga un warning e restituisce None.
    """
    if value is None:
        return None

    text = str(value).strip()
    if text == "":
        return None

    # Prima prova: come sta
    try:
        return float(text)
    except Exception:
        pass

    # Seconda prova: sostituisco la virgola col punto
    if "," in text:
        fixed = text.replace(",", ".")
        try:
            return float(fixed)
        except Exception:
            pass

    # Se non funziona nemmeno così, segnalo
    print(f"⚠️ Impossibile convertire {field_name}='{value}' nella riga: {row}")
    return None


def ensure_schema(cur):
    """
    Si assicura che la tabella restaurants esista
    con tutte le colonne necessarie.
    """
    # Tabella base
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS restaurants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            city TEXT NOT NULL,
            address TEXT,
            notes TEXT,
            source TEXT NOT NULL CHECK(source IN ('app', 'user')),
            lat REAL,
            lon REAL,
            rating REAL,
            last_update TEXT
        )
        """
    )

    # Migrazioni "dolci" per vecchi DB che non avevano le colonne
    for col_def in [
        ("lat", "REAL"),
        ("lon", "REAL"),
        ("rating", "REAL"),
        ("last_update", "TEXT"),
    ]:
        col_name, col_type = col_def
        try:
            cur.execute(f"ALTER TABLE restaurants ADD COLUMN {col_name} {col_type};")
            print(f"ℹ️ Aggiunta colonna mancante '{col_name}' alla tabella restaurants.")
        except sqlite3.OperationalError:
            # Colonna già esistente → nessun problema
            pass


def import_app_restaurants():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.cursor()

        # Mi assicuro che la tabella sia ok
        ensure_schema(cur)
        conn.commit()

        # Cancello i vecchi ristoranti source='app'
        print("🔄 Cancello vecchi ristoranti con source = 'app'...")
        cur.execute("DELETE FROM restaurants WHERE source = 'app';")
        conn.commit()

        print(f"📂 Leggo il file CSV: {CSV_PATH}")

        with open(CSV_PATH, newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)

            if reader.fieldnames is None:
                raise ValueError("Il CSV non ha intestazioni di colonna (riga 1 vuota?).")

            fieldnames = [f.strip() for f in reader.fieldnames]

            def has_field(name):
                return name in fieldnames

            required = {"name", "city", "address", "notes"}
            if not required.issubset(set(fieldnames)):
                raise ValueError(
                    f"Il CSV deve contenere almeno queste colonne: {sorted(required)}.\n"
                    f"Colonne trovate: {fieldnames}"
                )

            count = 0
            for row in reader:
                # Normalizzo le chiavi (per sicurezza)
                normalized_row = {k.strip(): v for k, v in row.items()}

                name = (normalized_row.get("name") or "").strip()
                city = (normalized_row.get("city") or "").strip()
                address = (normalized_row.get("address") or "").strip()
                notes = (normalized_row.get("notes") or "").strip()

                if not name or not city:
                    print(f"⚠️ Riga saltata (manca name o city): {normalized_row}")
                    continue

                # Lat / Lon / Rating / Last_update se presenti
                lat = safe_float(
                    normalized_row.get("lat") if has_field("lat") else None,
                    "lat",
                    normalized_row,
                )
                lon = safe_float(
                    normalized_row.get("lon") if has_field("lon") else None,
                    "lon",
                    normalized_row,
                )
                rating = safe_float(
                    normalized_row.get("rating") if has_field("rating") else None,
                    "rating",
                    normalized_row,
                )

                last_update = (
                    (normalized_row.get("last_update") or "").strip()
                    if has_field("last_update")
                    else ""
                )
                if last_update == "":
                    last_update = None

                cur.execute(
                    """
                    INSERT INTO restaurants
                        (name, city, address, notes, source, lat, lon, rating, last_update)
                    VALUES
                        (?,    ?,    ?,       ?,     'app',  ?,   ?,   ?,      ?)
                    """,
                    (name, city, address, notes, lat, lon, rating, last_update),
                )
                count += 1

        conn.commit()
        print(f"✅ Import completato. Ristoranti 'app' inseriti: {count}")


if __name__ == "__main__":
    import_app_restaurants()
