import sqlite3
from datetime import datetime, timedelta
import threading
import json

thread_local = threading.local()

def get_db_connection():
    if not hasattr(thread_local, "connection"):
        thread_local.connection = sqlite3.connect('users.db', timeout=20)
    return thread_local.connection

def close_db_connection():
    if hasattr(thread_local, "connection"):
        thread_local.connection.close()
        del thread_local.connection

class DatabaseConnection:
    def __enter__(self):
        self.conn = get_db_connection()
        return self.conn
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()

def init_db():
    with DatabaseConnection() as conn:
        c = conn.cursor()
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                subscription_status INTEGER DEFAULT 0,
                subscription_end_date TEXT,
                subscription_duration INTEGER DEFAULT 0,
                orders_enabled INTEGER DEFAULT 0,
                site INTEGER DEFAULT 0,
                vk INTEGER DEFAULT 0,
                tg INTEGER DEFAULT 0,
                registration_date TEXT NOT NULL,
                role TEXT DEFAULT 'user'
            )
        ''')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS sent_messages (
                message_id TEXT PRIMARY KEY,
                channel_id TEXT,
                source TEXT,
                text TEXT,
                media_path TEXT,
                sent_date TEXT,
                parsed_date TEXT
            )
        ''')
        
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
            admin_ids = config.get('admins', [])
            
            for admin_id in admin_ids:
                c.execute('SELECT 1 FROM users WHERE user_id = ?', (admin_id,))
                if not c.fetchone():
                    c.execute('''
                        INSERT INTO users 
                        (user_id, registration_date, role) 
                        VALUES (?, ?, ?)
                    ''', (admin_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'admin'))

def add_user(user_id: int, username: str):
    try:
        with DatabaseConnection() as conn:
            c = conn.cursor()
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute('''
                INSERT OR IGNORE INTO users 
                (user_id, username, registration_date, subscription_status, orders_enabled) 
                VALUES (?, ?, ?, 0, 0)
            ''', (user_id, username, current_time))
            return True
    except Exception as e:
        print(f"Ошибка при добавлении пользователя: {e}")
        return False

def get_user(user_id: int):
    try:
        with DatabaseConnection() as conn:
            c = conn.cursor()
            c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
            user = c.fetchone()
            
            if user:
                if user[2] and user[3]:
                    end_date = datetime.strptime(user[3], '%Y-%m-%d %H:%M:%S')
                    if end_date < datetime.now():
                        c.execute('''
                            UPDATE users 
                            SET subscription_status = 0, 
                                subscription_end_date = NULL, 
                                subscription_duration = 0 
                            WHERE user_id = ?
                        ''', (user_id,))
                        user = list(user)
                        user[2] = 0
                        user[3] = None
                        user[4] = 0

                return {
                    'user_id': user[0],
                    'username': user[1],
                    'subscription_status': bool(user[2]),
                    'subscription_end_date': user[3],
                    'subscription_duration': user[4],
                    'orders_enabled': bool(user[5]),
                    'site': bool(user[6]),
                    'vk': bool(user[7]),
                    'tg': bool(user[8]),
                    'registration_date': user[9],
                    'role': user[10]
                }
            return None
    except Exception as e:
        print(f"Ошибка при получении пользователя: {e}")
        return None

def user_exists(user_id: int):
    with DatabaseConnection() as conn:
        c = conn.cursor()
        c.execute('SELECT 1 FROM users WHERE user_id = ?', (user_id,))
        return bool(c.fetchone())

def is_admin(user_id: int) -> bool:
    with DatabaseConnection() as conn:
        c = conn.cursor()
        c.execute('SELECT role FROM users WHERE user_id = ?', (user_id,))
        result = c.fetchone()
        return result[0] == 'admin' if result else False

def set_admin(user_id: int, is_admin: bool = True):
    try:
        with DatabaseConnection() as conn:
            c = conn.cursor()
            role = 'admin' if is_admin else 'user'
            c.execute('''
                UPDATE users 
                SET role = ? 
                WHERE user_id = ?
            ''', (role, user_id))
            return True
    except Exception as e:
        print(f"Ошибка при изменении роли пользователя: {e}")
        return False

def set_subscription(user_id: int, duration_months: float):
    try:
        with DatabaseConnection() as conn:
            c = conn.cursor()
            
            c.execute('''
                SELECT subscription_status, subscription_end_date 
                FROM users 
                WHERE user_id = ?
            ''', (user_id,))
            current = c.fetchone()
            
            days = int(30 * duration_months)
            
            if current and current[0] and current[1]:
                current_end = datetime.strptime(current[1], '%Y-%m-%d %H:%M:%S')
                if current_end > datetime.now():
                    end_date = current_end + timedelta(days=days)
                else:
                    end_date = datetime.now() + timedelta(days=days)
            else:
                end_date = datetime.now() + timedelta(days=days)
                
            end_date_str = end_date.strftime('%Y-%m-%d %H:%M:%S')
            
            c.execute('''
                UPDATE users 
                SET subscription_status = 1,
                    subscription_end_date = ?,
                    subscription_duration = CASE 
                        WHEN subscription_status = 1 THEN subscription_duration + ?
                        ELSE ?
                    END
                WHERE user_id = ?
            ''', (end_date_str, duration_months, duration_months, user_id))
            return True
    except Exception as e:
        print(f"Ошибка при обновлении статуса подписки: {e}")
        return False

def toggle_orders(user_id: int, status: bool = None):
    try:
        with DatabaseConnection() as conn:
            c = conn.cursor()
            if status is None:
                c.execute('SELECT orders_enabled FROM users WHERE user_id = ?', (user_id,))
                current = c.fetchone()[0]
                new_status = 0 if current else 1
            else:
                new_status = 1 if status else 0
                
            c.execute('''
                UPDATE users 
                SET orders_enabled = ? 
                WHERE user_id = ?
            ''', (new_status, user_id))
            return True
    except Exception as e:
        print(f"Ошибка при изменении статуса заказов: {e}")
        return False

def update_sources(user_id: int, source_type: str, status: bool):
    if source_type not in ['site', 'vk', 'tg']:
        return False
        
    try:
        with DatabaseConnection() as conn:
            c = conn.cursor()
            c.execute(f'''
                UPDATE users 
                SET {source_type} = ? 
                WHERE user_id = ?
            ''', (1 if status else 0, user_id))
            return True
    except Exception as e:
        print(f"Ошибка при обновлении источника {source_type}: {e}")
        return False

def set_all_sources(user_id: int, status: bool):
    try:
        with DatabaseConnection() as conn:
            c = conn.cursor()
            c.execute('''
                UPDATE users 
                SET site = ?, vk = ?, tg = ? 
                WHERE user_id = ?
            ''', (1 if status else 0, 1 if status else 0, 1 if status else 0, user_id))
            return True
    except Exception as e:
        print(f"Ошибка при обновлении всех источников: {e}")
        return False

def get_all_subscribed_users():
    try:
        with DatabaseConnection() as conn:
            c = conn.cursor()
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            c.execute('''
                SELECT user_id, username, orders_enabled, tg, vk, site 
                FROM users 
                WHERE subscription_status = 1 
                AND (subscription_end_date IS NULL OR subscription_end_date > ?)
            ''', (current_time,))
            
            users = []
            for row in c.fetchall():
                users.append({
                    'user_id': row[0],
                    'username': row[1],
                    'orders_enabled': bool(row[2]),
                    'tg': bool(row[3]),
                    'vk': bool(row[4]),
                    'site': bool(row[5])
                })
            return users
            
    except Exception as e:
        print(f"Ошибка при получении списка пользователей: {str(e)}")
        return []

def add_sent_message(message_data: dict):
    try:
        with DatabaseConnection() as conn:
            c = conn.cursor()
            
            if message_data['source'] == 'hh':
                source_id = 'hh'
                message_id = str(message_data['vacancy_id'])
                text = f"{message_data['title']}\n{message_data.get('description', '')}"
            else:
                source_id = str(message_data.get('channel_id') or message_data.get('owner_id'))
                message_id = str(message_data['message_id'])
                text = message_data.get('text', '')
            
            unique_id = f"{message_data['source']}_{source_id}_{message_id}"
            
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute('''
                INSERT OR IGNORE INTO sent_messages 
                (message_id, channel_id, source, text, media_path, sent_date, parsed_date) 
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                unique_id,
                source_id,
                message_data['source'],
                text,
                message_data.get('media_path'),
                current_time,
                message_data.get('date')
            ))
            return True
    except Exception as e:
        print(f"Ошибка при добавлении сообщения в историю: {e}")
        return False

def is_message_sent(source: str, channel_id: str, message_id: str) -> bool:
    with DatabaseConnection() as conn:
        c = conn.cursor()
        unique_id = f"{source}_{channel_id}_{message_id}"
        c.execute('SELECT 1 FROM sent_messages WHERE message_id = ?', (unique_id,))
        return bool(c.fetchone())

def get_sent_messages_stats():
    with DatabaseConnection() as conn:
        c = conn.cursor()
        
        c.execute('SELECT COUNT(*) FROM sent_messages')
        total_count = c.fetchone()[0]
        
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        c.execute('SELECT COUNT(*) FROM sent_messages WHERE sent_date > ?', (yesterday,))
        last_24h_count = c.fetchone()[0]
        
        c.execute('SELECT source, COUNT(*) FROM sent_messages GROUP BY source')
        sources_stats = dict(c.fetchall())
        
        return {
            'total': total_count,
            'last_24h': last_24h_count,
            'by_source': sources_stats
        }

def cleanup_old_messages(days: int = 30):
    try:
        with DatabaseConnection() as conn:
            c = conn.cursor()
            cleanup_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
            c.execute('DELETE FROM sent_messages WHERE sent_date < ?', (cleanup_date,))
            return True
    except Exception as e:
        print(f"Ошибка при очистке старых сообщений: {e}")
        return False

def reset_subscription(user_id: int) -> bool:
    try:
        with DatabaseConnection() as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE users 
                SET subscription_status = 0,
                    subscription_end_date = NULL,
                    subscription_duration = 0
                WHERE user_id = ?
            """, (user_id,))
            return True
    except Exception as e:
        print(f"Ошибка при обнулении подписки: {str(e)}")
        return False

init_db()