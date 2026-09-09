import sqlite3

DB_PATH = 'fuel.db'

def create_tables():
    """Создаёт таблицы, если их нет."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.executescript('''
        CREATE TABLE IF NOT EXISTS stations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id VARCHAR(50) NOT NULL UNIQUE,
            source VARCHAR(20) NOT NULL DEFAULT 'gpn',
            name VARCHAR(255) NOT NULL,
            address TEXT NOT NULL,
            lat FLOAT,
            lon FLOAT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS fuel_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
            price_100 FLOAT,
            available_100 BOOLEAN,
            price_95plus FLOAT,
            available_95plus BOOLEAN,
            price_95 FLOAT,
            available_95 BOOLEAN,
            price_92 FLOAT,
            available_92 BOOLEAN,
            price_dt FLOAT,
            available_dt BOOLEAN,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_fuel_prices_station_updated 
        ON fuel_prices(station_id, updated_at DESC);
    ''')
    conn.commit()
    conn.close()
    print("✅ Таблицы созданы (или уже существуют).")

def insert_station_manual():
    """Ручное добавление станций в таблицу stations."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("\n--- Ручное заполнение таблицы stations ---")
    while True:
        print("\nВведите данные новой станции (или оставьте external_id пустым для выхода):")
        external_id = input("external_id (уникальный ID из API, например 9143): ").strip()
        if not external_id:
            print("Выход из режима ввода.")
            break

        name = input("Название заправки: ").strip()
        address = input("Адрес: ").strip()
        lat = input("Широта (lat, или оставьте пустым): ").strip()
        lon = input("Долгота (lon, или оставьте пустым): ").strip()
        source = input("Источник (по умолчанию 'gpn'): ").strip() or 'gpn'

        # Преобразуем lat/lon в float, если указаны
        lat_val = float(lat) if lat else None
        lon_val = float(lon) if lon else None

        try:
            cursor.execute('''
                INSERT INTO stations (external_id, source, name, address, lat, lon)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (external_id, source, name, address, lat_val, lon_val))
            conn.commit()
            print(f"✅ Станция '{name}' добавлена с ID = {cursor.lastrowid}.")
        except sqlite3.IntegrityError:
            print(f"❌ Ошибка: станция с external_id = {external_id} уже существует. Пропускаем.")

    conn.close()
    print("\nЗаполнение завершено.")

if __name__ == "__main__":
    create_tables()
    insert_station_manual()