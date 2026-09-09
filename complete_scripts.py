import sqlite3

DB_PATH = "fuel.db"  # путь к твоей базе


def add_vip_column():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Проверяем, есть ли колонка vip_until
    cursor.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in cursor.fetchall()]

    if 'vip_until' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN vip_until TIMESTAMP")
        conn.commit()
        print("✅ Колонка vip_until успешно добавлена в таблицу users")
    else:
        print("ℹ️ Колонка vip_until уже существует, ничего не делаю")

    conn.close()


if __name__ == "__main__":
    add_vip_column()