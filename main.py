import logging
import sqlite3
import asyncio
from urllib.parse import quote
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, LinkPreviewOptions
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import (
    BOT_TOKEN,
    DB_PATH,
    GROUP_CHAT_ID,
    OWNER_ID,
    CONTACT_INFO,
    GROUP_SHOW_STATUSES
)
from update_prices import update_all_stations

logging.basicConfig(level=logging.INFO)


# ===== ФУНКЦИИ РАБОТЫ С БД =====

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Создаём таблицу users, если её нет
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Проверяем, есть ли колонка vip_until
    cursor.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'vip_until' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN vip_until TIMESTAMP")
        conn.commit()
        logging.info("✅ Добавлена колонка vip_until в таблицу users")
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


def is_vip(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT vip_until FROM users WHERE telegram_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        try:
            vip_until = datetime.fromisoformat(row[0])
            return vip_until > datetime.now()
        except:
            return False
    return False


def get_vip_until(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT vip_until FROM users WHERE telegram_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        return row[0]
    return None


def set_vip(user_id: int, days: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    vip_until = (datetime.now() + timedelta(days=days)).isoformat()
    cursor.execute('''
        UPDATE users SET vip_until = ? WHERE telegram_id = ?
    ''', (vip_until, user_id))
    conn.commit()
    conn.close()


def remove_vip(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users SET vip_until = NULL WHERE telegram_id = ?
    ''', (user_id,))
    conn.commit()
    conn.close()


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


def format_all_stations(stations, show_statuses: bool = True):
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

        if show_statuses:
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
    builder.add(InlineKeyboardButton(text="⭐ Подписка", callback_data="subscription_info"))
    builder.adjust(3, 2, 1)
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
    show_statuses = is_vip(message.from_user.id)
    text = format_all_stations(stations, show_statuses)
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


@dp.message(Command("addvip"))
async def cmd_addvip(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ У вас нет прав на эту команду.")
        return

    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer(
            "❌ Формат: /addvip <id или @username> <время>\nПример: /addvip 123456789 30d\nПример: /addvip @john 2h\nПример: /addvip 123456789 15m")
        return

    identifier = args[1].strip()
    time_str = args[2].strip()

    # Парсим время
    try:
        if time_str.endswith('d'):
            days = int(time_str[:-1])
            seconds = days * 86400
        elif time_str.endswith('h'):
            hours = int(time_str[:-1])
            seconds = hours * 3600
        elif time_str.endswith('m'):
            minutes = int(time_str[:-1])
            seconds = minutes * 60
        else:
            days = int(time_str)
            seconds = days * 86400
    except:
        await message.answer("❌ Ошибка в формате времени. Примеры: 30d, 2h, 15m")
        return

    if seconds <= 0:
        await message.answer("❌ Время должно быть положительным.")
        return

    # Находим пользователя
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    user_id = None
    username = identifier.lstrip('@')
    if identifier.isdigit():
        cursor.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (int(identifier),))
        row = cursor.fetchone()
        if row:
            user_id = row[0]
    else:
        cursor.execute("SELECT telegram_id FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        if row:
            user_id = row[0]
    conn.close()

    if not user_id:
        await message.answer(f"❌ Пользователь с идентификатором '{identifier}' не найден.")
        return

    # Устанавливаем VIP
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT vip_until FROM users WHERE telegram_id = ?", (user_id,))
    row = cursor.fetchone()
    now = datetime.now()
    if row and row[0]:
        try:
            current_vip_until = datetime.fromisoformat(row[0])
            if current_vip_until > now:
                new_vip_until = current_vip_until + timedelta(seconds=seconds)
            else:
                new_vip_until = now + timedelta(seconds=seconds)
        except:
            new_vip_until = now + timedelta(seconds=seconds)
    else:
        new_vip_until = now + timedelta(seconds=seconds)

    cursor.execute('''
        UPDATE users SET vip_until = ? WHERE telegram_id = ?
    ''', (new_vip_until.isoformat(), user_id))
    conn.commit()
    conn.close()

    # Уведомляем пользователя
    try:
        await message.bot.send_message(
            user_id,
            f"🎉 Вам выдана VIP-подписка до {new_vip_until.strftime('%d.%m.%Y %H:%M')}!\n"
            f"Теперь вам доступны все статусы: ✅, ⏳, 🚚"
        )
    except:
        pass

    await message.answer(
        f"✅ Пользователю {identifier} выдана VIP-подписка до {new_vip_until.strftime('%d.%m.%Y %H:%M')}.")


@dp.message(Command("removevip"))
async def cmd_removevip(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ У вас нет прав на эту команду.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ Формат: /removevip <id или @username>\nПример: /removevip 123456789\nПример: /removevip @john")
        return

    identifier = args[1].strip()

    # Находим пользователя
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    user_id = None
    username = identifier.lstrip('@')
    if identifier.isdigit():
        cursor.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (int(identifier),))
        row = cursor.fetchone()
        if row:
            user_id = row[0]
    else:
        cursor.execute("SELECT telegram_id FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        if row:
            user_id = row[0]
    conn.close()

    if not user_id:
        await message.answer(f"❌ Пользователь с идентификатором '{identifier}' не найден.")
        return

    remove_vip(user_id)

    # Уведомляем пользователя
    try:
        await message.bot.send_message(
            user_id,
            f"❌ Ваша VIP-подписка была отключена администратором."
        )
    except:
        pass

    await message.answer(f"✅ VIP-подписка у пользователя {identifier} удалена.")


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
    show_statuses = is_vip(callback.from_user.id)
    text = format_all_stations(stations, show_statuses)
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=get_back_keyboard(),
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )


@dp.callback_query(F.data == "subscription_info")
async def callback_subscription_info(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    vip_until_str = get_vip_until(user_id)

    if vip_until_str:
        try:
            vip_until = datetime.fromisoformat(vip_until_str)
            if vip_until > datetime.now():
                status_text = f"✅ Активна до {vip_until.strftime('%d.%m.%Y %H:%M')}"
            else:
                status_text = "❌ Истекла"
        except:
            status_text = "❌ Ошибка данных"
    else:
        status_text = "❌ Неактивна"

    # Используем CONTACT_INFO как готовый текст
    text = CONTACT_INFO
    text += f"\n\n<b>Ваш статус:</b> {status_text}"

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


# ===== ФУНКЦИЯ ОТПРАВКИ В ГРУППУ =====

async def send_group_update(bot: Bot):
    try:
        stations = get_all_stations_with_fuel()
        text = format_all_stations(stations, show_statuses=GROUP_SHOW_STATUSES)
        await bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
        logging.info("✅ Отправлено новое сообщение в группу")
    except Exception as e:
        logging.error(f"❌ Ошибка при отправке сообщения в группу: {e}")


# ===== ФОНОВАЯ ЗАДАЧА =====

async def combined_group_updater(bot: Bot):
    while True:
        try:
            await asyncio.to_thread(update_all_stations)
            logging.info("✅ Фоновое обновление цен выполнено")
            await send_group_update(bot)
            logging.info("✅ Новое сообщение отправлено в группу")
        except Exception as e:
            logging.error(f"❌ Критическая ошибка в цикле обновления: {e}")
        await asyncio.sleep(3600)  # 1 час


# ===== ЗАПУСК =====

async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    logging.info("Бот запускается без прокси (прямое подключение)")
    asyncio.create_task(combined_group_updater(bot))
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"Ошибка при запуске: {e}")


if __name__ == "__main__":
    asyncio.run(main())