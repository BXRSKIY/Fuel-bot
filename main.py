import logging
import sqlite3
import asyncio
from urllib.parse import quote
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, LinkPreviewOptions
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import BOT_TOKEN, DB_PATH, GROUP_CHAT_ID, OWNER_ID
from update_prices import update_all_stations

logging.basicConfig(level=logging.INFO)

# ===== ФУНКЦИИ РАБОТЫ С БД (пользователи) =====

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def add_user(telegram_id: int, username: str = None, first_name: str = None, last_name: str = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR IGNORE INTO users (telegram_id, username, first_name, last_name)
        VALUES (?, ?, ?, ?)
    ''', (telegram_id, username, first_name, last_name))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id FROM users")
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

# ===== ФУНКЦИИ РАБОТЫ С БД (топливо) =====

def get_stations_by_fuel(fuel_type: str):
    mapping = {
        '100': ('price_100', 'available_100', 'delivery_100'),
        '95+': ('price_95plus', 'available_95plus', 'delivery_95plus'),
        '95': ('price_95', 'available_95', 'delivery_95'),
        '92': ('price_92', 'available_92', 'delivery_92'),
        'ДТ': ('price_dt', 'available_dt', 'delivery_dt'),
    }
    price_col, avail_col, delivery_col = mapping.get(fuel_type)
    if not price_col:
        return []
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    query = f'''
        SELECT s.name, s.address, p.{price_col} as price, p.updated_at, s.lat, s.lon,
               p.{avail_col} as available, p.{delivery_col} as delivery
        FROM stations s
        JOIN fuel_prices p ON s.id = p.station_id
        WHERE p.{avail_col} = 1
        ORDER BY s.name
    '''
    cursor.execute(query)
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_all_stations_with_fuel():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    query = '''
        SELECT s.id, s.name, s.address, s.lat, s.lon,
               p.available_100, p.delivery_100,
               p.available_95plus, p.delivery_95plus,
               p.available_95, p.delivery_95,
               p.available_92, p.delivery_92,
               p.available_dt, p.delivery_dt
        FROM stations s
        JOIN fuel_prices p ON s.id = p.station_id
        ORDER BY s.name
    '''
    cursor.execute(query)
    rows = cursor.fetchall()
    conn.close()

    fuel_fields = [
        ('available_100', 'delivery_100', '100'),
        ('available_95plus', 'delivery_95plus', '95+'),
        ('available_95', 'delivery_95', '95'),
        ('available_92', 'delivery_92', '92'),
        ('available_dt', 'delivery_dt', 'ДТ'),
    ]

    result = []
    for row in rows:
        station_id, name, address, lat, lon, *values = row
        available = []
        soon = []
        in_transit = []

        for i, (avail_field, delivery_field, display_name) in enumerate(fuel_fields):
            avail = values[i * 2]
            delivery = values[i * 2 + 1]

            if avail:
                available.append(display_name)
            elif delivery == 'soon':
                soon.append(display_name)
            elif delivery == 'yes':
                in_transit.append(display_name)

        if available or soon or in_transit:
            result.append((name, address, lat, lon, available, soon, in_transit))
    return result

def is_valid_url(text: str) -> bool:
    return text.startswith(('http://', 'https://'))

def format_stations_list(fuel_type: str, stations):
    if not stations:
        return f"😕 Нет станций с топливом '{fuel_type}' в наличии."

    parts = [f"🚗 Станции с топливом '{fuel_type}' в наличии:\n"]
    for idx, (name, address, price, updated, lat, lon, available, delivery) in enumerate(stations, 1):
        if updated:
            try:
                dt = datetime.fromisoformat(updated)
                formatted_time = dt.strftime("%y.%m.%d %H:%M")
            except:
                formatted_time = updated
        else:
            formatted_time = "неизвестно"

        if lat is not None and lon is not None:
            map_url = f"https://yandex.ru/maps/?pt={lon},{lat}&z=17"
        elif is_valid_url(address):
            map_url = address
        elif address:
            map_url = f"https://yandex.ru/maps/?text={quote(address)}"
        else:
            map_url = None

        link = f'<a href="{map_url}">{name}</a>' if map_url else name

        parts.append(
            f"{idx}. {link}\n"
            f"   💰 {price} руб.\n"
            f"   🕒 {formatted_time}\n"
        )
        if idx >= 20:
            parts.append("... и ещё много станций. Уточните поиск по городу (пока не реализовано).")
            break
    return "\n".join(parts)

def format_all_stations(stations):
    if not stations:
        return "😕 Нет станций с топливом в наличии."

    parts = ["📍 Все заправки с топливом в наличии:\n"]
    for idx, (name, address, lat, lon, available, soon, in_transit) in enumerate(stations, 1):
        if lat is not None and lon is not None:
            map_url = f"https://yandex.ru/maps/?pt={lon},{lat}&z=17"
        elif is_valid_url(address):
            map_url = address
        elif address:
            map_url = f"https://yandex.ru/maps/?text={quote(address)}"
        else:
            map_url = None

        link = f'<a href="{map_url}">{name}</a>' if map_url else name

        parts.append(f"{idx}. {link}")

        if available:
            parts.append(f"   ✅{', '.join(available)}")
        if soon:
            parts.append(f"   ⏳{', '.join([f'{f} (≈ 2 часа)' for f in soon])}")
        if in_transit:
            parts.append(f"   🚚{', '.join(in_transit)}")

        parts.append("")

    return "\n".join(parts)

# ===== КЛАВИАТУРЫ =====

def get_fuel_choice_keyboard():
    builder = InlineKeyboardBuilder()
    fuel_types = ["100", "95+", "95", "92", "ДТ"]
    for ft in fuel_types:
        builder.add(InlineKeyboardButton(text=ft, callback_data=f"fuel_{ft}"))
    builder.add(InlineKeyboardButton(text="📍 Все заправки", callback_data="all_stations"))
    builder.adjust(3, 2)
    return builder.as_markup()

def get_back_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_fuel"))
    return builder.as_markup()

# ===== ДИСПЕТЧЕР =====
dp = Dispatcher()

# ===== ОБРАБОТЧИКИ =====

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    add_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name
    )
    await message.answer(
        "👋 Привет! Я бот для поиска топлива на заправках Газпромнефть.\n"
        "Используй команду /fuel, чтобы выбрать тип топлива или посмотреть все заправки."
    )

@dp.message(Command("fuel"))
async def cmd_fuel(message: types.Message):
    add_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name
    )
    await message.answer(
        "Выберите тип топлива или посмотрите все заправки:",
        reply_markup=get_fuel_choice_keyboard()
    )

@dp.message(Command("all"))
async def cmd_all(message: types.Message):
    add_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name
    )
    stations = get_all_stations_with_fuel()
    text = format_all_stations(stations)
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=get_back_keyboard(),
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ У вас нет прав на эту команду.")
        return

    text = message.text[len("/broadcast"):].strip()
    if not text:
        await message.answer("❌ Укажите текст уведомления после команды.\nПример: /broadcast Внимание! Завтра акция.")
        return

    users = get_all_users()
    if not users:
        await message.answer("⚠️ Нет ни одного пользователя в базе.")
        return

    sent = 0
    failed = 0
    for user_id in users:
        try:
            await message.bot.send_message(chat_id=user_id, text=text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            failed += 1
            logging.warning(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    await message.answer(
        f"✅ Рассылка завершена.\n"
        f"📤 Отправлено: {sent}\n"
        f"❌ Ошибок: {failed}"
    )

@dp.callback_query(F.data.startswith("fuel_"))
async def callback_fuel(callback: types.CallbackQuery):
    add_user(
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name
    )
    fuel_type = callback.data.split("_")[1]
    await callback.answer()
    stations = get_stations_by_fuel(fuel_type)
    text = format_stations_list(fuel_type, stations)
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=get_back_keyboard(),
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

@dp.callback_query(F.data == "all_stations")
async def callback_all_stations(callback: types.CallbackQuery):
    add_user(
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name
    )
    await callback.answer()
    stations = get_all_stations_with_fuel()
    text = format_all_stations(stations)
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=get_back_keyboard(),
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

@dp.callback_query(F.data == "back_to_fuel")
async def callback_back_to_fuel(callback: types.CallbackQuery):
    add_user(
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name
    )
    await callback.answer()
    await callback.message.edit_text(
        "Выберите тип топлива или посмотрите все заправки:",
        reply_markup=get_fuel_choice_keyboard()
    )

# ===== ФУНКЦИЯ ОТПРАВКИ СООБЩЕНИЯ В ГРУППЕ (всегда новое) =====

async def send_group_update(bot: Bot):
    """Отправляет НОВОЕ сообщение в группу (без редактирования)."""
    try:
        stations = get_all_stations_with_fuel()
        text = format_all_stations(stations)

        await bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
        logging.info("✅ Новое сообщение отправлено в группу")
    except Exception as e:
        logging.error(f"❌ Ошибка при отправке сообщения в группу: {e}")

# ===== ЕДИНАЯ ФОНОВАЯ ЗАДАЧА =====

async def combined_group_updater(bot: Bot):
    """Каждый час обновляет цены и отправляет новое сообщение в группу."""
    while True:
        try:
            # 1. Обновляем цены
            try:
                await asyncio.to_thread(update_all_stations)
                logging.info("✅ Фоновое обновление цен выполнено")
            except Exception as e:
                logging.error(f"❌ Ошибка при обновлении цен: {e}")

            # 2. Отправляем НОВОЕ сообщение в группу
            await send_group_update(bot)

        except Exception as e:
            logging.error(f"❌ Критическая ошибка в цикле обновления: {e}")

        await asyncio.sleep(3600)  # 1 час

# ===== ЗАПУСК =====

async def main():
    init_db()

    bot = Bot(token=BOT_TOKEN)
    logging.info("Бот запускается без прокси (прямое подключение)")

    # Запускаем фоновую задачу (она сразу обновит цены и отправит сообщение)
    asyncio.create_task(combined_group_updater(bot))

    try:
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"Ошибка при запуске: {e}")

if __name__ == "__main__":
    asyncio.run(main())