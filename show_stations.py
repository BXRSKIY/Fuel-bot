import sqlite3

def get_stations_by_fuel(fuel_type):
    mapping = {
        '100': ('price_100', 'available_100'),
        '95+': ('price_95plus', 'available_95plus'),
        '95': ('price_95', 'available_95'),
        '92': ('price_92', 'available_92'),
        'ДТ': ('price_dt', 'available_dt'),
    }

    fuel_type = fuel_type.strip()
    if fuel_type not in mapping:
        print(f"❌ Неизвестный тип топлива: '{fuel_type}'")
        print(f"Доступные варианты: {', '.join(mapping.keys())}")
        return

    price_col, avail_col = mapping[fuel_type]

    conn = sqlite3.connect('fuel.db')
    cursor = conn.cursor()

    # Выбираем только станции, где топливо есть в наличии (available = 1)
    query = f'''
        SELECT s.name, s.address, p.{price_col} as price, p.updated_at
        FROM stations s
        JOIN fuel_prices p ON s.id = p.station_id
        WHERE p.{avail_col} = 1
        ORDER BY s.name
    '''
    cursor.execute(query)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print(f"😕 Нет станций с топливом '{fuel_type}' в наличии.")
        return

    print(f"\n🚗 Станции с топливом '{fuel_type}' в наличии:\n")
    print("=" * 90)
    for name, address, price, updated in rows:
        print(f"📍 {name}")
        print(f"   Адрес: {address}")
        print(f"   💰 Цена: {price} руб.")
        print(f"   🕒 Обновлено: {updated}")
        print("-" * 90)

if __name__ == "__main__":
    fuel = input("Введите тип топлива (100, 95+, 95, 92, ДТ): ").strip()
    get_stations_by_fuel(fuel)