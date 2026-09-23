import logging
import sqlite3
import asyncio
from urllib.parse import quote
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton, LinkPreviewOptions,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from config import (
    BOT_TOKEN, DB_PATH, GROUP_CHAT_ID, OWNER_ID,
    CONTACT_INFO, GROUP_SHOW_STATUSES
)
from update_prices import update_all_stations

logging.basicConfig(level=logging.INFO)

# Глобальная переменная для бота (используется в фоновых задачах)
bot = None

# ===== ПОДКЛЮЧЕНИЕ К БД С ЗАЩИТОЙ ОТ БЛОКИРОВОК =====

def get_db():
    """Возвращает соединение с БД с настройками для параллельной работы."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

# ===== FSM =====

class AddManualStation(StatesGroup):
    waiting_for_name = State()
    waiting_for_url = State()

# ===== ИНИЦИАЛИЗАЦИЯ БД =====

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    # users
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute("PRAGMA table_info(users)")
    cols = [c[1] for c in cursor.fetchall()]
    if 'vip_until' not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN vip_until TIMESTAMP")

    # manual_stations
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS manual_stations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            lat REAL,
            lon REAL,
            map_url TEXT,
            available_100 INTEGER DEFAULT 0,
            available_95plus INTEGER DEFAULT 0,
            available_95 INTEGER DEFAULT 0,
            available_92 INTEGER DEFAULT 0,
            available_dt INTEGER DEFAULT 0,
            is_hidden INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Миграции
    cursor.execute("PRAGMA table_info(manual_stations)")
    mcols = [c[1] for c in cursor.fetchall()]
    if 'is_hidden' not in mcols:
        cursor.execute("ALTER TABLE manual_stations ADD COLUMN is_hidden INTEGER DEFAULT 0")
        logging.info("✅ Добавлена колонка is_hidden в manual_stations")
    if 'updated_at' not in mcols:
        cursor.execute("ALTER TABLE manual_stations ADD COLUMN updated_at TIMESTAMP")
        logging.info("✅ Добавлена колонка updated_at в manual_stations")
    if 'map_url' not in mcols:
        cursor.execute("ALTER TABLE manual_stations ADD COLUMN map_url TEXT")
        logging.info("✅ Добавлена колонка map_url в manual_stations")

    conn.commit()
    conn.close()

# ===== ПОЛЬЗОВАТЕЛИ =====

def add_user(tid, username=None, first=None, last=None):
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT OR IGNORE INTO users (telegram_id, username, first_name, last_name)
                 VALUES (?, ?, ?, ?)''', (tid, username, first, last))
    conn.commit()
    conn.close()

def get_all_users():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT telegram_id FROM users")
    r = [x[0] for x in c.fetchall()]
    conn.close()
    return r

def is_vip(uid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT vip_until FROM users WHERE telegram_id = ?", (uid,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        try:
            return datetime.fromisoformat(row[0]) > datetime.now()
        except:
            return False
    return False

def get_vip_until(uid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT vip_until FROM users WHERE telegram_id = ?", (uid,))
    row = c.fetchone()
    conn.close()
    return row[0] if row and row[0] else None

def remove_vip(uid):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET vip_until = NULL WHERE telegram_id = ?", (uid,))
    conn.commit()
    conn.close()

# ===== РУЧНЫЕ СТАНЦИИ =====

def add_manual_station(name, map_url=None, lat=None, lon=None):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO manual_stations (name, map_url, lat, lon) VALUES (?, ?, ?, ?)",
              (name, map_url, lat, lon))
    sid = c.lastrowid
    conn.commit()
    conn.close()
    return sid

def get_manual_stations():
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT id, name, lat, lon, map_url,
                 available_100, available_95plus, available_95, available_92, available_dt,
                 is_hidden, updated_at
                 FROM manual_stations ORDER BY name''')
    rows = c.fetchall()
    conn.close()
    return rows

def get_manual_station_by_id(sid):
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT id, name, lat, lon, map_url,
                 available_100, available_95plus, available_95, available_92, available_dt,
                 is_hidden FROM manual_stations WHERE id = ?''', (sid,))
    row = c.fetchone()
    conn.close()
    return row

def toggle_manual_fuel(sid, fuel_key):
    col = f"available_{fuel_key}"
    if col not in ['available_100','available_95plus','available_95','available_92','available_dt']:
        return
    conn = get_db()
    c = conn.cursor()
    c.execute(f"SELECT {col} FROM manual_stations WHERE id = ?", (sid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return
    new_val = 0 if row[0] else 1
    c.execute(f"UPDATE manual_stations SET {col} = ?, updated_at = ? WHERE id = ?",
              (new_val, datetime.now().isoformat(), sid))
    conn.commit()
    conn.close()

def set_manual_fuel(sid, fuel_key, value):
    """Устанавливает наличие топлива (value=1 — есть, value=0 — нет)."""
    col = f"available_{fuel_key}"
    if col not in ['available_100','available_95plus','available_95','available_92','available_dt']:
        return False
    conn = get_db()
    c = conn.cursor()
    c.execute(f"UPDATE manual_stations SET {col} = ?, updated_at = ? WHERE id = ?",
              (value, datetime.now().isoformat(), sid))
    conn.commit()
    conn.close()
    return True

def set_manual_hidden(sid, value):
    """Устанавливает скрытие станции (1 — скрыть, 0 — показать)."""
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE manual_stations SET is_hidden = ?, updated_at = ? WHERE id = ?",
              (value, datetime.now().isoformat(), sid))
    conn.commit()
    conn.close()

def fuel_key_from_input(text):
    """Преобразует ввод пользователя в ключ колонки БД."""
    mapping = {
        '100': '100',
        '95+': '95plus', '95plus': '95plus', '95плюс': '95plus',
        '95': '95',
        '92': '92',
        'дт': 'dt', 'dt': 'dt', 'дизель': 'dt', 'дтл': 'dt',
    }
    return mapping.get(text.strip().lower())

def fuel_name_from_key(key):
    """Человеческое название топлива по ключу."""
    mapping = {
        '100': '100', '95plus': '95+',
        '95': '95', '92': '92', 'dt': 'ДТ'
    }
    return mapping.get(key, key)

def toggle_manual_hidden(sid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT is_hidden FROM manual_stations WHERE id = ?", (sid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    new_val = 0 if row[0] else 1
    c.execute("UPDATE manual_stations SET is_hidden = ?, updated_at = ? WHERE id = ?",
              (new_val, datetime.now().isoformat(), sid))
    conn.commit()
    conn.close()
    return new_val

def delete_manual_station(sid):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM manual_stations WHERE id = ?", (sid,))
    conn.commit()
    conn.close()

def get_manual_map_url(lat, lon, map_url):
    """Возвращает URL карты: сначала map_url, потом по координатам."""
    if map_url:
        return map_url
    if lat is not None and lon is not None:
        return f"https://yandex.ru/maps/?pt={lon},{lat}&z=17"
    return None

# ===== API СТАНЦИИ =====

def get_stations_by_fuel(fuel_type):
    mapping = {
        '100': ('price_100', 'available_100', 'delivery_100'),
        '95+': ('price_95plus', 'available_95plus', 'delivery_95plus'),
        '95': ('price_95', 'available_95', 'delivery_95'),
        '92': ('price_92', 'available_92', 'delivery_92'),
        'ДТ': ('price_dt', 'available_dt', 'delivery_dt'),
    }
    price_col, avail_col, delivery_col = mapping.get(fuel_type, (None, None, None))
    if not price_col:
        return []
    conn = get_db()
    c = conn.cursor()
    c.execute(f'''SELECT s.name, s.address, p.{price_col}, p.updated_at, s.lat, s.lon,
                  p.{avail_col}, p.{delivery_col}
                  FROM stations s JOIN fuel_prices p ON s.id = p.station_id
                  WHERE p.{avail_col} = 1 ORDER BY s.name''')
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_stations_with_fuel():
    """Возвращает (api_stations, manual_stations) — два отдельных списка."""
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT s.id, s.name, s.address, s.lat, s.lon,
                 p.available_100, p.delivery_100,
                 p.available_95plus, p.delivery_95plus,
                 p.available_95, p.delivery_95,
                 p.available_92, p.delivery_92,
                 p.available_dt, p.delivery_dt
                 FROM stations s JOIN fuel_prices p ON s.id = p.station_id
                 ORDER BY s.name''')
    rows = c.fetchall()
    conn.close()

    fuel_fields = [
        ('available_100', 'delivery_100', '100'),
        ('available_95plus', 'delivery_95plus', '95+'),
        ('available_95', 'delivery_95', '95'),
        ('available_92', 'delivery_92', '92'),
        ('available_dt', 'delivery_dt', 'ДТ'),
    ]

    api_stations = []
    for row in rows:
        _, name, address, lat, lon, *vals = row
        avail, soon, transit = [], [], []
        for i, (_, _, dn) in enumerate(fuel_fields):
            a, d = vals[i*2], vals[i*2+1]
            if a: avail.append(dn)
            elif d == 'soon': soon.append(dn)
            elif d == 'yes': transit.append(dn)
        if avail or soon or transit:
            api_stations.append((name, address, lat, lon, avail, soon, transit))

    # Ручные (не скрытые)
    manual_stations = []
    for row in get_manual_stations():
        (sid, name, lat, lon, map_url,
         a100, a95plus, a95, a92, adt, is_hidden, _) = row
        if is_hidden:
            continue
        avail = []
        if a100: avail.append('100')
        if a95plus: avail.append('95+')
        if a95: avail.append('95')
        if a92: avail.append('92')
        if adt: avail.append('ДТ')
        if avail:
            manual_stations.append((name, map_url, lat, lon, avail, [], []))

    return api_stations, manual_stations

def is_valid_url(text):
    return text.startswith(('http://', 'https://'))

# ===== ФОРМАТИРОВАНИЕ =====

def format_stations_list(fuel_type, stations):
    if not stations:
        return f"😕 Нет станций с топливом '{fuel_type}' в наличии."
    parts = [f"🚗 Станции с топливом '{fuel_type}' в наличии:\n"]
    for idx, (name, address, price, updated, lat, lon, _, _) in enumerate(stations, 1):
        try:
            ftime = datetime.fromisoformat(updated).strftime("%d.%m.%y %H:%M")
        except:
            ftime = updated or "неизвестно"
        if lat is not None and lon is not None:
            url = f"https://yandex.ru/maps/?pt={lon},{lat}&z=17"
        elif is_valid_url(address):
            url = address
        elif address:
            url = f"https://yandex.ru/maps/?text={quote(address)}"
        else:
            url = None
        link = f'<a href="{url}">{name}</a>' if url else name
        parts.append(f"{idx}. {link}\n   💰 {price} руб.\n   🕒 {ftime}\n")
        if idx >= 20:
            parts.append("... и ещё много станций.")
            break
    return "\n".join(parts)

def format_all_stations(stations, show_statuses=True):
    """
    stations — кортеж (api_stations, manual_stations)
    Выводит двумя секциями без разделителя.
    """
    api_stations, manual_stations = stations

    if not api_stations and not manual_stations:
        return "😕 Нет станций с топливом в наличии."

    parts = ["📍 Все заправки с топливом в наличии:\n"]
    counter = 0

    # ===== Секция 1: API-заправки =====
    for (name, address, lat, lon, avail, soon, transit) in api_stations:
        counter += 1
        if lat is not None and lon is not None:
            url = f"https://yandex.ru/maps/?pt={lon},{lat}&z=17"
        elif is_valid_url(address):
            url = address
        elif address:
            url = f"https://yandex.ru/maps/?text={quote(address)}"
        else:
            url = None
        link = f'<a href="{url}">{name}</a>' if url else name
        parts.append(f"{counter}. {link}")
        if avail: parts.append(f"   ✅{', '.join(avail)}")
        if show_statuses:
            if soon: parts.append(f"   ⏳{', '.join([f'{f} (≈ 2 часа)' for f in soon])}")
            if transit: parts.append(f"   🚚{', '.join(transit)}")
        parts.append("")

    # ===== Секция 2: Ручные заправки =====
    if manual_stations:
        parts.append("<b>📝 Заправки без авто-обновления:</b>\n")
        for (name, map_url, lat, lon, avail, soon, transit) in manual_stations:
            counter += 1
            url = get_manual_map_url(lat, lon, map_url)
            link = f'<a href="{url}">{name}</a>' if url else name
            parts.append(f"{counter}. {link}")
            if avail: parts.append(f"   ✅{', '.join(avail)}")
            parts.append("")

    return "\n".join(parts)

# ===== КЛАВИАТУРЫ =====

def get_admin_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⚙️ Админ-панель")]],
        resize_keyboard=True
    )

def get_fuel_choice_keyboard(user_id: int = None):
    """Меню выбора топлива. Для владельца добавляется кнопка админ-панели."""
    b = InlineKeyboardBuilder()
    for ft in ["100", "95+", "95", "92", "ДТ"]:
        b.add(InlineKeyboardButton(text=ft, callback_data=f"fuel_{ft}"))
    b.add(InlineKeyboardButton(text="📍 Все заправки", callback_data="all_stations"))
    b.add(InlineKeyboardButton(text="⭐ Подписка", callback_data="subscription_info"))
    if user_id == OWNER_ID:
        b.add(InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_menu"))
        b.adjust(3, 2, 1, 1, 1)
    else:
        b.adjust(3, 2, 1, 1)
    return b.as_markup()

def get_back_keyboard():
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_fuel"))
    return b.as_markup()

def get_admin_menu_keyboard():
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="📋 Список ручных заправок", callback_data="manual_list"))
    b.add(InlineKeyboardButton(text="➕ Добавить заправку", callback_data="manual_add"))
    b.add(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_fuel"))
    b.adjust(1)
    return b.as_markup()

def get_manual_list_keyboard():
    stations = get_manual_stations()
    b = InlineKeyboardBuilder()
    for i, row in enumerate(stations, 1):
        b.add(InlineKeyboardButton(text=f"{i}", callback_data=f"mopen_{row[0]}"))
    b.add(InlineKeyboardButton(text="➕ Добавить", callback_data="manual_add"))
    b.add(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_menu"))
    if stations:
        rows_count = (len(stations) + 4) // 5
        b.adjust(*([5] * rows_count), 1, 1)
    else:
        b.adjust(1, 1)
    return b.as_markup()

def get_manual_station_keyboard(sid, a100, a95plus, a95, a92, adt, is_hidden):
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(
        text=f"{'✅' if a100 else '❌'} 100", callback_data=f"mtoggle_{sid}_100"))
    b.add(InlineKeyboardButton(
        text=f"{'✅' if a95plus else '❌'} 95+", callback_data=f"mtoggle_{sid}_95plus"))
    b.add(InlineKeyboardButton(
        text=f"{'✅' if a95 else '❌'} 95", callback_data=f"mtoggle_{sid}_95"))
    b.add(InlineKeyboardButton(
        text=f"{'✅' if a92 else '❌'} 92", callback_data=f"mtoggle_{sid}_92"))
    b.add(InlineKeyboardButton(
        text=f"{'✅' if adt else '❌'} ДТ", callback_data=f"mtoggle_{sid}_dt"))
    hide_text = "👁 Показать" if is_hidden else "👁 Скрыть"
    b.add(InlineKeyboardButton(text=hide_text, callback_data=f"mhidden_{sid}"))
    b.add(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"mdel_{sid}"))
    b.add(InlineKeyboardButton(text="🔙 К списку", callback_data="manual_list"))
    b.adjust(3, 2, 1, 1, 1)
    return b.as_markup()

# ===== ДИСПЕТЧЕР =====

dp = Dispatcher()

# ===== СТАРТ / FUEL =====

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    add_user(message.from_user.id, message.from_user.username,
             message.from_user.first_name, message.from_user.last_name)
    kb = get_admin_reply_keyboard() if message.from_user.id == OWNER_ID else None
    await message.answer(
        "👋 Привет! Я бот для поиска топлива на заправках Газпромнефть.\n"
        "Используй /fuel, чтобы выбрать топливо или посмотреть все заправки.",
        reply_markup=kb
    )

@dp.message(Command("fuel"))
async def cmd_fuel(message: types.Message):
    add_user(message.from_user.id, message.from_user.username,
             message.from_user.first_name, message.from_user.last_name)
    await message.answer(
        "Выберите тип топлива или посмотрите все заправки:",
        reply_markup=get_fuel_choice_keyboard(message.from_user.id)
    )

@dp.message(Command("all"))
async def cmd_all(message: types.Message):
    add_user(message.from_user.id, message.from_user.username,
             message.from_user.first_name, message.from_user.last_name)
    stations = get_all_stations_with_fuel()
    text = format_all_stations(stations, is_vip(message.from_user.id))
    await message.answer(text, parse_mode="HTML",
                         reply_markup=get_back_keyboard(),
                         link_preview_options=LinkPreviewOptions(is_disabled=True))

# ===== АДМИН-ПАНЕЛЬ =====

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Нет прав")
        return
    await message.answer("⚙️ Админ-панель:", reply_markup=get_admin_menu_keyboard())

@dp.message(F.text == "⚙️ Админ-панель")
async def handle_admin_button(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    await message.answer("⚙️ Админ-панель:", reply_markup=get_admin_menu_keyboard())

@dp.callback_query(F.data == "admin_menu")
async def cb_admin_menu(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text("⚙️ Админ-панель:", reply_markup=get_admin_menu_keyboard())

# ===== СПИСОК РУЧНЫХ СТАНЦИЙ =====

@dp.callback_query(F.data == "manual_list")
async def cb_manual_list(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    await callback.answer()
    stations = get_manual_stations()
    if not stations:
        b = InlineKeyboardBuilder()
        b.add(InlineKeyboardButton(text="➕ Добавить", callback_data="manual_add"))
        b.add(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_menu"))
        b.adjust(1)
        await callback.message.edit_text("📭 Ручных заправок нет.", reply_markup=b.as_markup())
        return

    text = "📍 <b>Ручные заправки:</b>\n\n"
    for i, row in enumerate(stations, 1):
        (sid, name, lat, lon, map_url,
         a100, a95plus, a95, a92, adt, is_hidden, _) = row
        url = get_manual_map_url(lat, lon, map_url)
        link = f'<a href="{url}">{name}</a>' if url else name
        fuels = []
        if a100: fuels.append('100')
        if a95plus: fuels.append('95+')
        if a95: fuels.append('95')
        if a92: fuels.append('92')
        if adt: fuels.append('ДТ')
        fuels_str = ", ".join(fuels) if fuels else "нет топлива"
        hidden_mark = " 👁 Скрыта" if is_hidden else ""
        text += f"<b>{i}.</b> {link}{hidden_mark}\n   ✅{fuels_str}\n\n"

    text += "Нажмите на номер для управления:"
    await callback.message.edit_text(text, parse_mode="HTML",
                                     reply_markup=get_manual_list_keyboard(),
                                     link_preview_options=LinkPreviewOptions(is_disabled=True))

# ===== КАРТОЧКА СТАНЦИИ =====

@dp.callback_query(F.data.startswith("mopen_"))
async def cb_manual_open(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    await callback.answer()
    sid = int(callback.data.split("_")[1])
    st = get_manual_station_by_id(sid)
    if not st:
        await callback.message.edit_text("❌ Не найдена")
        return
    (sid, name, lat, lon, map_url,
     a100, a95plus, a95, a92, adt, is_hidden) = st
    url = get_manual_map_url(lat, lon, map_url)
    status = "👁 Скрыта" if is_hidden else "✅ Видима"
    link = f'<a href="{url}">{name}</a>' if url else name
    text = (
        f"📍 <b>{link}</b>\n"
        f"ID: {sid}\n"
        f"Статус: {status}\n\n"
        f"<b>Переключите наличие топлива:</b>"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=get_manual_station_keyboard(sid, a100, a95plus, a95, a92, adt, is_hidden),
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

@dp.callback_query(F.data.startswith("mtoggle_"))
async def cb_manual_toggle(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    parts = callback.data.split("_")
    sid = int(parts[1])
    fuel_key = parts[2]
    toggle_manual_fuel(sid, fuel_key)
    await callback.answer("✅")

    st = get_manual_station_by_id(sid)
    if not st:
        return
    (sid, name, lat, lon, map_url,
     a100, a95plus, a95, a92, adt, is_hidden) = st
    url = get_manual_map_url(lat, lon, map_url)
    status = "👁 Скрыта" if is_hidden else "✅ Видима"
    link = f'<a href="{url}">{name}</a>' if url else name
    text = (
        f"📍 <b>{link}</b>\n"
        f"ID: {sid}\n"
        f"Статус: {status}\n\n"
        f"<b>Переключите наличие топлива:</b>"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=get_manual_station_keyboard(sid, a100, a95plus, a95, a92, adt, is_hidden),
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

@dp.callback_query(F.data.startswith("mhidden_"))
async def cb_manual_hidden(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    sid = int(callback.data.split("_")[1])
    new_val = toggle_manual_hidden(sid)
    await callback.answer("👁 Показана" if new_val == 0 else "👁 Скрыта")

    st = get_manual_station_by_id(sid)
    if not st:
        return
    (sid, name, lat, lon, map_url,
     a100, a95plus, a95, a92, adt, is_hidden) = st
    url = get_manual_map_url(lat, lon, map_url)
    status = "👁 Скрыта" if is_hidden else "✅ Видима"
    link = f'<a href="{url}">{name}</a>' if url else name
    text = (
        f"📍 <b>{link}</b>\n"
        f"ID: {sid}\n"
        f"Статус: {status}\n\n"
        f"<b>Переключите наличие топлива:</b>"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=get_manual_station_keyboard(sid, a100, a95plus, a95, a92, adt, is_hidden),
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

@dp.callback_query(F.data.startswith("mdel_"))
async def cb_manual_delete(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    sid = int(callback.data.split("_")[1])
    st = get_manual_station_by_id(sid)
    if not st:
        await callback.answer("Не найдена", show_alert=True)
        return
    name = st[1]
    delete_manual_station(sid)
    await callback.answer(f"🗑 «{name}» удалена", show_alert=True)
    callback.data = "manual_list"
    await cb_manual_list(callback)

# ===== ДОБАВЛЕНИЕ СТАНЦИИ (FSM) =====

@dp.callback_query(F.data == "manual_add")
async def cb_manual_add(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AddManualStation.waiting_for_name)
    await callback.message.answer(
        "➕ <b>Добавление ручной заправки</b>\n\n"
        "Введите <b>название</b> станции:\n"
        "(например: Лукойл на Ленина)",
        parse_mode="HTML"
    )

@dp.message(Command("addstation"))
async def cmd_addstation(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Нет прав")
        return
    await state.set_state(AddManualStation.waiting_for_name)
    await message.answer("➕ Введите <b>название</b> станции:", parse_mode="HTML")

@dp.message(AddManualStation.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("❌ Введите название")
        return
    await state.update_data(name=name)
    await state.set_state(AddManualStation.waiting_for_url)
    await message.answer(
        "🔗 Теперь скиньте <b>ссылку на Яндекс.Карты</b> этой заправки.\n\n"
        "Как получить:\n"
        "1. Откройте Яндекс.Карты\n"
        "2. Найдите нужную заправку\n"
        "3. Нажмите «Поделиться» → «Скопировать ссылку»\n"
        "4. Пришлите ссылку сюда",
        parse_mode="HTML"
    )

@dp.message(AddManualStation.waiting_for_url)
async def process_url(message: types.Message, state: FSMContext):
    url = message.text.strip()
    if not is_valid_url(url):
        await message.answer("❌ Это не похоже на ссылку. Пришлите ссылку, начинающуюся с http:// или https://")
        return

    data = await state.get_data()
    name = data.get("name")
    # Сохраняем только map_url, lat/lon оставляем NULL
    sid = add_manual_station(name, map_url=url)
    await state.clear()

    link = f'<a href="{url}">{name}</a>'
    await message.answer(
        f'✅ Станция добавлена!\n\n'
        f'📍 <b>{link}</b>\n'
        f"ID: {sid}\n\n"
        f"Теперь переключите наличие топлива:",
        parse_mode="HTML",
        reply_markup=get_manual_station_keyboard(sid, 0, 0, 0, 0, 0, 0),
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

# ===== БЫСТРЫЕ КОМАНДЫ ДЛЯ РУЧНЫХ ЗАПРАВОК =====

@dp.message(Command("addfuel"))
async def cmd_addfuel(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Нет прав")
        return

    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer(
            "❌ Формат: <code>/addfuel id_заправки тип_топлива</code>\n"
            "Пример: <code>/addfuel 1 95</code>\n\n"
            "Доступные типы: <code>100</code>, <code>95+</code>, <code>95</code>, <code>92</code>, <code>ДТ</code>",
            parse_mode="HTML"
        )
        return

    try:
        sid = int(args[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return

    fuel_key = fuel_key_from_input(args[2])
    if not fuel_key:
        await message.answer("❌ Неизвестный тип топлива. Доступные: 100, 95+, 95, 92, ДТ")
        return

    st = get_manual_station_by_id(sid)
    if not st:
        await message.answer(f"❌ Заправка с ID {sid} не найдена")
        return

    set_manual_fuel(sid, fuel_key, 1)
    await message.answer(
        f"✅ Добавлено <b>{fuel_name_from_key(fuel_key)}</b> на заправку «{st[1]}»",
        parse_mode="HTML"
    )

@dp.message(Command("removefuel"))
async def cmd_removefuel(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Нет прав")
        return

    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer(
            "❌ Формат: <code>/removefuel id_заправки тип_топлива</code>\n"
            "Пример: <code>/removefuel 1 95</code>",
            parse_mode="HTML"
        )
        return

    try:
        sid = int(args[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return

    fuel_key = fuel_key_from_input(args[2])
    if not fuel_key:
        await message.answer("❌ Неизвестный тип топлива. Доступные: 100, 95+, 95, 92, ДТ")
        return

    st = get_manual_station_by_id(sid)
    if not st:
        await message.answer(f"❌ Заправка с ID {sid} не найдена")
        return

    set_manual_fuel(sid, fuel_key, 0)
    await message.answer(
        f"✅ Убрано <b>{fuel_name_from_key(fuel_key)}</b> с заправки «{st[1]}»",
        parse_mode="HTML"
    )

@dp.message(Command("show"))
async def cmd_show(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Нет прав")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ Формат: <code>/show id_заправки</code>", parse_mode="HTML")
        return

    try:
        sid = int(args[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return

    st = get_manual_station_by_id(sid)
    if not st:
        await message.answer(f"❌ Заправка с ID {sid} не найдена")
        return

    set_manual_hidden(sid, 0)
    await message.answer(f"👁 Заправка «{st[1]}» теперь видна в «Все заправки»")

@dp.message(Command("hide"))
async def cmd_hide(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Нет прав")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ Формат: <code>/hide id_заправки</code>", parse_mode="HTML")
        return

    try:
        sid = int(args[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return

    st = get_manual_station_by_id(sid)
    if not st:
        await message.answer(f"❌ Заправка с ID {sid} не найдена")
        return

    set_manual_hidden(sid, 1)
    await message.answer(f"👁 Заправка «{st[1]}» скрыта из «Все заправки»")

@dp.message(Command("delstation"))
async def cmd_delstation(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Нет прав")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Формат: /delstation <id>")
        return

    sid = int(args[1])
    st = get_manual_station_by_id(sid)
    if not st:
        await message.answer(f"❌ Заправка с ID {sid} не найдена")
        return

    delete_manual_station(sid)
    await message.answer(f"✅ Заправка «{st[1]}» удалена.")

@dp.message(Command("listmanual"))
async def cmd_listmanual(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Нет прав")
        return

    stations = get_manual_stations()
    if not stations:
        await message.answer("📭 Нет ручных станций.\n\nДобавьте через /addstation")
        return

    text = "📍 <b>Ручные станции:</b>\n\n"
    for row in stations:
        mid, name = row[0], row[1]
        text += f"<b>ID {mid}:</b> {name}\n\n"
    text += "Нажмите на станцию для управления:"
    await message.answer(text, parse_mode="HTML", reply_markup=get_manual_list_keyboard())

# ===== API CALLBACKS =====

@dp.callback_query(F.data.startswith("fuel_"))
async def cb_fuel(callback: types.CallbackQuery):
    add_user(callback.from_user.id, callback.from_user.username,
             callback.from_user.first_name, callback.from_user.last_name)
    ft = callback.data.split("_")[1]
    await callback.answer()
    stations = get_stations_by_fuel(ft)
    await callback.message.edit_text(
        format_stations_list(ft, stations), parse_mode="HTML",
        reply_markup=get_back_keyboard(),
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

@dp.callback_query(F.data == "all_stations")
async def cb_all_stations(callback: types.CallbackQuery):
    add_user(callback.from_user.id, callback.from_user.username,
             callback.from_user.first_name, callback.from_user.last_name)
    await callback.answer()
    stations = get_all_stations_with_fuel()
    await callback.message.edit_text(
        format_all_stations(stations, is_vip(callback.from_user.id)),
        parse_mode="HTML", reply_markup=get_back_keyboard(),
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

@dp.callback_query(F.data == "subscription_info")
async def cb_sub(callback: types.CallbackQuery):
    await callback.answer()
    v = get_vip_until(callback.from_user.id)
    if v:
        try:
            vu = datetime.fromisoformat(v)
            st = f"✅ Активна до {vu.strftime('%d.%m.%Y %H:%M')}" if vu > datetime.now() else "❌ Истекла"
        except:
            st = "❌ Ошибка"
    else:
        st = "❌ Неактивна"
    await callback.message.edit_text(
        CONTACT_INFO + f"\n\n<b>Ваш статус:</b> {st}",
        parse_mode="HTML", reply_markup=get_back_keyboard(),
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

@dp.callback_query(F.data == "back_to_fuel")
async def cb_back(callback: types.CallbackQuery):
    add_user(callback.from_user.id, callback.from_user.username,
             callback.from_user.first_name, callback.from_user.last_name)
    await callback.answer()
    await callback.message.edit_text(
        "Выберите тип топлива или посмотрите все заправки:",
        reply_markup=get_fuel_choice_keyboard(callback.from_user.id)
    )

# ===== VIP / BROADCAST =====

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔")
        return
    text = message.text[len("/broadcast"):].strip()
    if not text:
        await message.answer("❌ /broadcast текст")
        return
    users = get_all_users()
    sent = failed = 0
    for uid in users:
        try:
            await message.bot.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.05)
        except:
            failed += 1
    await message.answer(f"✅ Отправлено: {sent}, ошибок: {failed}")

@dp.message(Command("addvip"))
async def cmd_addvip(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔")
        return
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer("❌ /addvip <id|@user> <30d|2h|15m>")
        return
    ident, tstr = args[1].strip(), args[2].strip()
    try:
        if tstr.endswith('d'): sec = int(tstr[:-1])*86400
        elif tstr.endswith('h'): sec = int(tstr[:-1])*3600
        elif tstr.endswith('m'): sec = int(tstr[:-1])*60
        else: sec = int(tstr)*86400
    except:
        await message.answer("❌ формат: 30d, 2h, 15m")
        return
    conn = get_db()
    c = conn.cursor()
    uid = None
    if ident.isdigit():
        c.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (int(ident),))
    else:
        c.execute("SELECT telegram_id FROM users WHERE username=?", (ident.lstrip('@'),))
    r = c.fetchone()
    if r:
        uid = r[0]
    if not uid:
        conn.close()
        await message.answer("❌ не найден")
        return
    c.execute("SELECT vip_until FROM users WHERE telegram_id=?", (uid,))
    row = c.fetchone()
    now = datetime.now()
    if row and row[0]:
        try:
            cur = datetime.fromisoformat(row[0])
            new = (cur if cur > now else now) + timedelta(seconds=sec)
        except:
            new = now + timedelta(seconds=sec)
    else:
        new = now + timedelta(seconds=sec)
    c.execute("UPDATE users SET vip_until=? WHERE telegram_id=?", (new.isoformat(), uid))
    conn.commit()
    conn.close()
    try:
        await message.bot.send_message(uid, f"🎉 VIP до {new.strftime('%d.%m.%Y %H:%M')}")
    except:
        pass
    await message.answer(f"✅ выдано до {new.strftime('%d.%m.%Y %H:%M')}")

@dp.message(Command("addvipall"))
async def cmd_addvipall(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Нет прав")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ Формат: <code>/addvipall &lt;время&gt;</code>\n\n"
            "Примеры:\n"
            "<code>/addvipall 7d</code> — всем на 7 дней\n"
            "<code>/addvipall 2h</code> — всем на 2 часа\n"
            "<code>/addvipall 30m</code> — всем на 30 минут",
            parse_mode="HTML"
        )
        return

    tstr = args[1].strip()

    try:
        if tstr.endswith('d'):
            seconds = int(tstr[:-1]) * 86400
            human = f"{int(tstr[:-1])} дн."
        elif tstr.endswith('h'):
            seconds = int(tstr[:-1]) * 3600
            human = f"{int(tstr[:-1])} ч."
        elif tstr.endswith('m'):
            seconds = int(tstr[:-1]) * 60
            human = f"{int(tstr[:-1])} мин."
        else:
            seconds = int(tstr) * 86400
            human = f"{int(tstr)} дн."
    except:
        await message.answer("❌ Ошибка формата. Примеры: 7d, 2h, 30m")
        return

    if seconds <= 0:
        await message.answer("❌ Время должно быть положительным")
        return

    users = get_all_users()
    if not users:
        await message.answer("⚠️ В базе нет пользователей")
        return

    conn = get_db()
    c = conn.cursor()
    now = datetime.now()
    updated = 0

    for uid in users:
        c.execute("SELECT vip_until FROM users WHERE telegram_id = ?", (uid,))
        row = c.fetchone()
        if row and row[0]:
            try:
                cur = datetime.fromisoformat(row[0])
                new_vip = (cur if cur > now else now) + timedelta(seconds=seconds)
            except:
                new_vip = now + timedelta(seconds=seconds)
        else:
            new_vip = now + timedelta(seconds=seconds)

        c.execute("UPDATE users SET vip_until = ? WHERE telegram_id = ?",
                  (new_vip.isoformat(), uid))
        updated += 1

    conn.commit()
    conn.close()

    await message.answer(
        f"✅ VIP выдан <b>{updated}</b> пользователям на <b>{human}</b>",
        parse_mode="HTML"
    )

    asyncio.create_task(notify_vip_all(users))

@dp.message(Command("removevip"))
async def cmd_removevip(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ /removevip <id|@user>")
        return
    ident = args[1].strip()
    conn = get_db()
    c = conn.cursor()
    if ident.isdigit():
        c.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (int(ident),))
    else:
        c.execute("SELECT telegram_id FROM users WHERE username=?", (ident.lstrip('@'),))
    r = c.fetchone()
    conn.close()
    if not r:
        await message.answer("❌ не найден")
        return
    remove_vip(r[0])
    try:
        await message.bot.send_message(r[0], "❌ VIP отключён")
    except:
        pass
    await message.answer("✅ VIP удалён")

# ===== ФОНОВЫЕ УВЕДОМЛЕНИЯ =====

async def notify_vip_all(users):
    """Рассылает уведомление о выданном VIP всем пользователям."""
    global bot
    if bot is None:
        return
    text = (
        "🎉 <b>Вам выдана VIP-подписка!</b>\n\n"
        "Теперь вам доступны расширенные статусы топлива:\n"
        "✅ — в наличии\n"
        "⏳ — будет через 1-2 часа\n"
        "🚚 — в пути\n\n"
        "Проверьте в разделе «Все заправки»."
    )
    sent = 0
    failed = 0
    for uid in users:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except:
            failed += 1
    logging.info(f"📢 Уведомления VIP: отправлено {sent}, ошибок {failed}")

# ===== ФОНОВОЕ ОБНОВЛЕНИЕ =====

async def background_update():
    while True:
        try:
            await asyncio.to_thread(update_all_stations)
            logging.info("✅ Обновление цен выполнено")
        except Exception as e:
            logging.error(f"❌ Ошибка: {e}")
        await asyncio.sleep(300)

# ===== ЗАПУСК =====

async def main():
    global bot
    init_db()
    bot = Bot(token=BOT_TOKEN)
    logging.info("Бот запускается")
    asyncio.create_task(background_update())
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"Ошибка запуска: {e}")

if __name__ == "__main__":
    asyncio.run(main())