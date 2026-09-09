import sqlite3
import requests
from datetime import datetime, timedelta
from config import DB_PATH

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
    'Referer': 'https://gpnbonus.ru/fuel/refuel-map',
    'Origin': 'https://gpnbonus.ru',
    'Connection': 'keep-alive',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
}

# Попытка импорта зонных модулей
try:
    from zoneinfo import ZoneInfo
    HAS_ZONEINFO = True
except ImportError:
    HAS_ZONEINFO = False

try:
    import pytz
    HAS_PYTZ = True
except ImportError:
    HAS_PYTZ = False

def get_now_msk():
    if HAS_ZONEINFO:
        tz = ZoneInfo('Europe/Moscow')
        now = datetime.now(tz)
        return now.isoformat(timespec='microseconds')
    elif HAS_PYTZ:
        tz = pytz.timezone('Europe/Moscow')
        now = datetime.now(tz)
        return now.isoformat(timespec='microseconds')
    else:
        now_utc = datetime.utcnow()
        now_msk = now_utc + timedelta(hours=3)
        return now_msk.isoformat(timespec='microseconds')

def fetch_prices(station_id):
    url = f"https://gpnbonus.ru/api/stations/{station_id}"
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        resp = session.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        else:
            print(f"Ошибка {resp.status_code} для станции {station_id}")
            return None
    except Exception as e:
        print(f"Ошибка запроса для {station_id}: {e}")
        return None

def parse_prices(raw_data):
    """Извлекает цены, наличие и статус доставки из ответа API."""
    if not isinstance(raw_data, dict):
        return {
            'price_100': None, 'available_100': None, 'delivery_100': None,
            'price_95plus': None, 'available_95plus': None, 'delivery_95plus': None,
            'price_95': None, 'available_95': None, 'delivery_95': None,
            'price_92': None, 'available_92': None, 'delivery_92': None,
            'price_dt': None, 'available_dt': None, 'delivery_dt': None,
        }

    result = {
        'price_100': None, 'available_100': None, 'delivery_100': None,
        'price_95plus': None, 'available_95plus': None, 'delivery_95plus': None,
        'price_95': None, 'available_95': None, 'delivery_95': None,
        'price_92': None, 'available_92': None, 'delivery_92': None,
        'price_dt': None, 'available_dt': None, 'delivery_dt': None,
    }

    data_list = raw_data.get('data')
    if not isinstance(data_list, list):
        return result

    for item in data_list:
        if not isinstance(item, dict):
            continue
        product = item.get('product')
        if not isinstance(product, dict):
            continue
        title = product.get('title')

        price_info = item.get('price')
        if isinstance(price_info, dict):
            price = price_info.get('price')
        else:
            price = None

        rest = item.get('rest')
        if isinstance(rest, dict):
            avail = rest.get('avail', False)
            delivery = rest.get('delivery')
        else:
            avail = False
            delivery = None

        # Маппинг title -> ключи результата
        if title == 'Бензин АИ-100 брендированный':
            result['price_100'] = price
            result['available_100'] = avail
            result['delivery_100'] = delivery
        elif title == 'Бензин АИ-95 брендированный':
            result['price_95plus'] = price
            result['available_95plus'] = avail
            result['delivery_95plus'] = delivery
        elif title == 'Бензин АИ-95':
            result['price_95'] = price
            result['available_95'] = avail
            result['delivery_95'] = delivery
        elif title == 'Бензин АИ-92':
            result['price_92'] = price
            result['available_92'] = avail
            result['delivery_92'] = delivery
        elif title == 'Дизельное топливо летнее':
            result['price_dt'] = price
            result['available_dt'] = avail
            result['delivery_dt'] = delivery

    return result

def update_all_stations():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, external_id FROM stations WHERE source = 'gpn'")
    stations = cursor.fetchall()

    for station_id, external_id in stations:
        raw = fetch_prices(external_id)
        if raw is None:
            continue
        prices = parse_prices(raw)

        cursor.execute("SELECT id FROM fuel_prices WHERE station_id = ?", (station_id,))
        existing = cursor.fetchone()

        now_msk = get_now_msk()

        if existing:
            cursor.execute('''
                UPDATE fuel_prices SET
                    price_100 = ?, available_100 = ?, delivery_100 = ?,
                    price_95plus = ?, available_95plus = ?, delivery_95plus = ?,
                    price_95 = ?, available_95 = ?, delivery_95 = ?,
                    price_92 = ?, available_92 = ?, delivery_92 = ?,
                    price_dt = ?, available_dt = ?, delivery_dt = ?,
                    updated_at = ?
                WHERE station_id = ?
            ''', (
                prices['price_100'], prices['available_100'], prices['delivery_100'],
                prices['price_95plus'], prices['available_95plus'], prices['delivery_95plus'],
                prices['price_95'], prices['available_95'], prices['delivery_95'],
                prices['price_92'], prices['available_92'], prices['delivery_92'],
                prices['price_dt'], prices['available_dt'], prices['delivery_dt'],
                now_msk,
                station_id
            ))
            print(f"Обновлены цены для станции ID {station_id} (external_id {external_id})")
        else:
            cursor.execute('''
                INSERT INTO fuel_prices (
                    station_id,
                    price_100, available_100, delivery_100,
                    price_95plus, available_95plus, delivery_95plus,
                    price_95, available_95, delivery_95,
                    price_92, available_92, delivery_92,
                    price_dt, available_dt, delivery_dt,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                station_id,
                prices['price_100'], prices['available_100'], prices['delivery_100'],
                prices['price_95plus'], prices['available_95plus'], prices['delivery_95plus'],
                prices['price_95'], prices['available_95'], prices['delivery_95'],
                prices['price_92'], prices['available_92'], prices['delivery_92'],
                prices['price_dt'], prices['available_dt'], prices['delivery_dt'],
                now_msk
            ))
            print(f"Добавлены цены для станции ID {station_id} (первое обновление)")

    conn.commit()
    conn.close()
    print("Все цены обновлены!")

if __name__ == "__main__":
    update_all_stations()