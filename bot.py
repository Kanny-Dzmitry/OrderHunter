import json
import asyncio
import os
import random
import signal
import subprocess
import psutil
import re
from telethon import TelegramClient, events, Button
from telethon.errors import MessageNotModifiedError
from datetime import datetime, timedelta
import glob
from database import (
    add_user, 
    get_user, 
    user_exists, 
    is_admin as db_is_admin, 
    set_subscription,
    toggle_orders,
    update_sources,
    set_all_sources,
    set_admin,
    get_all_subscribed_users,
    add_sent_message,
    get_sent_messages_stats,
    cleanup_old_messages,
    is_message_sent,
    reset_subscription
)
import sqlite3

# Загружаем конфигурацию с учетом отсутствия основного файла
config_file = 'config.json'
if not os.path.exists(config_file):
    config_file = 'config.example.json'
    print(f"⚠️ Файл config.json не найден, используем {config_file}")
    print("⚠️ Создайте файл config.json на основе примера и заполните его вашими данными")

with open(config_file, 'r', encoding='utf-8') as f:
    config = json.load(f)

for source in config['sources'].values():
    if source.get('enabled', False):
        for folder in [source['data_folder'], source['messages_folder'], source.get('media_folder', '')]:
            if folder and not os.path.exists(folder):
                os.makedirs(folder)

# Проверяем наличие необходимых API ключей
if config_file == 'config.example.json' or config.get('api_id') == "YOUR_TELEGRAM_API_ID":
    print("❌ Ошибка: API ключи не настроены. Отредактируйте config.json")
    exit(1)

bot = TelegramClient('tg_bot_session', config['api_id'], config['api_hash'])

is_running = True
parser_process = None
parser_task = None

def signal_handler(sig, frame):
    global is_running, parser_process, parser_task
    print("\nЗавершение работы бота...")
    is_running = False
    
    if parser_process:
        kill_process_tree(parser_process.pid)
        parser_process = None
    
    if parser_task:
        parser_task.cancel()
        parser_task = None

    os._exit(0)

signal.signal(signal.SIGINT, signal_handler)

async def save_config():
    with open('config.json', 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=4)

def kill_process_tree(pid):
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            child.kill()
        parent.kill()
    except psutil.NoSuchProcess:
        pass

async def stop_parser():
    global parser_process, parser_task, is_running
    
    if parser_task:
        parser_task.cancel()
        parser_task = None
        return True
    return False

async def send_order_to_users(message):
    try:
        if message['source'] == 'telegram':
            source_id = str(message['channel_id'])
            source_info = config['sources']['telegram']['channels'].get(source_id, {})
        else:
            source_id = str(message['owner_id'])
            source_info = config['sources']['vk']['groups'].get(source_id, {})
            
        added_time = source_info.get('added_time', 0)
        
        if datetime.now().timestamp() - added_time < 240:
            print(f"Пропускаем сообщение из недавно добавленного источника {source_id}")
            return
            
        if is_message_sent(message['source'], source_id, str(message['message_id'])):
            print(f"Сообщение {message['message_id']} из {message['source']} {source_id} уже было отправлено")
            return
            
        users = get_all_subscribed_users()
        
        sent_to_users = False
        
        for user in users:
            if not user['orders_enabled']:
                continue
                
            if message['source'] == 'telegram' and not user['tg']:
                continue
            elif message['source'] == 'vk' and not user['vk']:
                continue
            elif message['source'] == 'site' and not user['site']:
                continue
            
            source_emoji = "📢" if message['source'] == 'telegram' else "📱"
            formatted_message = f"""
{source_emoji} Новый заказ из {message['source'].title()}

📝 Текст:
{message['text']}
"""
            try:
                if message.get('media_path') and os.path.exists(message['media_path']):
                    await bot.send_message(user['user_id'], formatted_message, file=message['media_path'])
                else:
                    await bot.send_message(user['user_id'], formatted_message)
                sent_to_users = True
            except Exception as e:
                print(f"Ошибка при отправке сообщения пользователю {user['user_id']}: {str(e)}")
                
        if sent_to_users:
            add_sent_message(message)
                
    except Exception as e:
        print(f"Ошибка при рассылке заказа: {str(e)}")

async def run_parser():
    global parser_process, parser_task
    try:
        if parser_task and not parser_task.done():
            return "⚠️ Парсер уже запущен"
            
        parser_task = asyncio.create_task(parser_loop())
        return "✅ Парсер запущен"
        
    except Exception as e:
        print(f"Ошибка при запуске парсера: {str(e)}")
        return "❌ Ошибка при запуске парсера"

def clean_html(html_text: str) -> str:
    if not html_text:
        return ""
        
    html_text = html_text.replace('</p>', '\n')
    html_text = html_text.replace('<br />', '\n')
    html_text = html_text.replace('<br/>', '\n')
    html_text = html_text.replace('<br>', '\n')
    html_text = html_text.replace('</li>', '\n')
    html_text = html_text.replace('</ul>', '\n')
    html_text = html_text.replace('</strong>', '*')
    html_text = html_text.replace('<strong>', '*')
    
    clean_text = re.sub(r'<[^>]+>', '', html_text)
    clean_text = re.sub(r'\n\s*\n', '\n\n', clean_text)
    clean_text = '\n'.join(line.strip() for line in clean_text.splitlines())
    
    return clean_text.strip()

async def process_new_messages(source):
    try:
        users = get_all_subscribed_users()
        if not users:
            print("Нет пользователей с активной подпиской")
            return

        messages_folder = config['sources'][source]['messages_folder']
        print(f"\n📂 Проверяем папку {messages_folder} для источника {source}")
        
        message_files = glob.glob(os.path.join(messages_folder, 'messages_*.json'))
        if not message_files:
            print("❌ Нет файлов с сообщениями")
            return
            
        message_files.sort(key=os.path.getctime, reverse=True)
        latest_file = message_files[0]
        print(f"📄 Обрабатываем файл: {latest_file}")
        
        try:
            with open(latest_file, 'r', encoding='utf-8') as f:
                messages = json.load(f)
                print(f"📨 Найдено {len(messages)} сообщений в файле")
                
            for message in messages:
                if source == 'telegram':
                    source_id = str(message['channel_id'])
                    message_id = str(message['message_id'])
                elif source == 'vk':
                    source_id = str(message['owner_id'])
                    message_id = str(message['message_id'])
                elif source == 'hh':
                    source_id = 'hh'
                    message_id = str(message['vacancy_id'])
                    print(f"\n💼 Обработка вакансии HH {message_id}:")
                    print(f"📝 Заголовок: {message.get('title', 'Нет заголовка')}")
                else:
                    continue
                
                if is_message_sent(source, source_id, message_id):
                    print(f"✓ Сообщение {message_id} из {source} {source_id} уже было отправлено")
                    continue
                else:
                    print(f"🆕 Найдено новое сообщение {message_id} из {source} {source_id}")
                
                if source == 'telegram':
                    text = f"📱 Новый заказ из Telegram\n\n{message['text']}"
                elif source == 'vk':
                    text = f"💻 Новый заказ из VK\n\n{message['text']}"
                elif source == 'hh':
                    description = clean_html(message.get('description', ''))
                    text = (f"💼 Новая вакансия с HH.ru\n\n"
                           f"🔹 {message['title']}\n"
                           f"💰 {message['salary']}\n"
                           f"🏢 {message['company']}\n\n"
                           f"📝 {description}\n\n"
                           f"🔗 {message['link']}")
                else:
                    continue
                
                sent_to_users = False
                
                for user in users:
                    try:
                        if source == 'telegram' and not user['tg']:
                            continue
                        elif source == 'vk' and not user['vk']:
                            continue
                        elif source == 'hh' and not user['site']:
                            print(f"👤 Пользователь {user['user_id']} не подписан на сайты, пропускаем")
                            continue
                            
                        if not user['orders_enabled']:
                            print(f"👤 У пользователя {user['user_id']} отключены уведомления, пропускаем")
                            continue
                            
                        if message.get('media_path') and os.path.exists(message['media_path']):
                            await bot.send_file(user['user_id'], 
                                              message['media_path'],
                                              caption=text[:1024])
                        else:
                            await bot.send_message(user['user_id'], text)
                            
                        sent_to_users = True
                        print(f"✅ Сообщение {message_id} отправлено пользователю {user['user_id']}")
                        await asyncio.sleep(0.5)
                        
                    except Exception as e:
                        print(f"❌ Ошибка при отправке сообщения пользователю {user['user_id']}: {str(e)}")
                        continue
                
                if sent_to_users:
                    try:
                        add_sent_message(message)
                        print(f"✅ Сообщение {message_id} из {source} {source_id} успешно отправлено и сохранено в базе")
                    except Exception as e:
                        print(f"❌ Ошибка при сохранении информации об отправленном сообщении: {str(e)}")
                else:
                    print(f"ℹ️ Сообщение {message_id} не было отправлено ни одному пользователю")
                
        except Exception as e:
            print(f"❌ Ошибка при обработке файла {latest_file}: {str(e)}")
            return
            
    except Exception as e:
        print(f"❌ Ошибка при обработке новых сообщений: {str(e)}")

async def cleanup_channel_data(channel_id):
    try:
        media_files_to_delete = set()
        messages_folder = config['sources']['telegram']['messages_folder']
        
        for filename in os.listdir(messages_folder):
            if filename.endswith('.json'):
                file_path = os.path.join(messages_folder, filename)
                with open(file_path, 'r', encoding='utf-8') as f:
                    messages = json.load(f)
                
                for msg in messages:
                    if msg['channel_id'] == channel_id and msg.get('media_path'):
                        media_files_to_delete.add(msg['media_path'])
                
                filtered_messages = [msg for msg in messages if msg['channel_id'] != channel_id]
                
                if filtered_messages:
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump(filtered_messages, f, ensure_ascii=False, indent=4)
                else:
                    os.remove(file_path)
                    print(f"Удален пустой файл: {filename}")
        
        deleted_files = 0
        for media_path in media_files_to_delete:
            try:
                if os.path.exists(media_path):
                    os.remove(media_path)
                    deleted_files += 1
            except Exception as e:
                print(f"Ошибка при удалении файла {media_path}: {str(e)}")
        
        return True, f"Данные канала успешно удалены (удалено {deleted_files} медиафайлов)"
    except Exception as e:
        return False, f"Ошибка при удалении данных канала: {str(e)}"

async def get_random_message():
    try:
        all_messages = []
        for source_name, source in config['sources'].items():
            if not source.get('enabled', False):
                continue
                
            message_files = [f for f in os.listdir(source['messages_folder']) if f.endswith('.json')]
            for file_name in message_files:
                file_path = os.path.join(source['messages_folder'], file_name)
                with open(file_path, 'r', encoding='utf-8') as f:
                    messages = json.load(f)
                    all_messages.extend(messages)
        
        if not all_messages:
            return None, "Нет сохраненных сообщений"
            
        message = random.choice(all_messages)
        
        source_emoji = "📢" if message['source'] == 'telegram' else "📱"
        formatted_message = f"""
{source_emoji} Случайное сообщение из {message['source'].title()} канала {message['channel_id']}

📝 Текст:
{message['text']}
"""
        return message, formatted_message
                
    except Exception as e:
        return None, f"Произошла ошибка при чтении сообщений: {str(e)}"

async def get_last_message():
    try:
        latest_file = None
        latest_time = 0
        source_folder = None
        
        for source_name, source in config['sources'].items():
            if not source.get('enabled', False):
                continue
                
            message_files = [f for f in os.listdir(source['messages_folder']) if f.endswith('.json')]
            for file_name in message_files:
                file_path = os.path.join(source['messages_folder'], file_name)
                file_time = os.path.getctime(file_path)
                if file_time > latest_time:
                    latest_time = file_time
                    latest_file = file_name
                    source_folder = source['messages_folder']
        
        if not latest_file:
            return None, "Нет сохраненных сообщений"
            
        with open(os.path.join(source_folder, latest_file), 'r', encoding='utf-8') as f:
            messages = json.load(f)
            
        if not messages:
            return None, "Файл с сообщениями пуст"
            
        message = messages[0]
        
        source_emoji = "📢" if message['source'] == 'telegram' else "📱"
        formatted_message = f"""
{source_emoji} Последнее сообщение из {message['source'].title()} канала {message['channel_id']}

📝 Текст:
{message['text']}
"""
        return message, formatted_message
                
    except Exception as e:
        return None, f"Произошла ошибка при чтении сообщений: {str(e)}"

def is_admin(user_id):
    return db_is_admin(user_id)

async def get_admin_buttons():
    global parser_process
    base_buttons = [
        [Button.inline("➕ Добавить канал", b"add_channel")],
        [Button.inline("➖ Удалить канал", b"remove_channel")],
        [Button.inline("📋 Список каналов", b"list_channels")],
        [Button.inline("💎 Выдать подписку", b"grant_subscription")]
    ]
    
    if parser_process and parser_process.returncode is None:
        base_buttons.append([Button.inline("🛑 Остановить парсер", b"stop_parser")])
    else:
        base_buttons.append([Button.inline("🔄 Запустить парсер", b"run_parser")])
    
    return base_buttons

async def add_channel_with_filters(event, channel_id):
    try:
        if 'sources' not in config:
            config['sources'] = {}
        if 'telegram' not in config['sources']:
            config['sources']['telegram'] = {'enabled': True, 'channels': {}}
        
        config['sources']['telegram']['channels'][str(channel_id)] = {
            'include_filters': [],
            'exclude_filters': [],
            'active': True,
            'added_time': datetime.now().timestamp()
        }
        await save_config()
        
        buttons = [
            [Button.inline("➕ Добавить слова для совпадения", "add_include_" + str(channel_id))],
            [Button.inline("➖ Добавить слова для исключения", "add_exclude_" + str(channel_id))],
            [Button.inline("✅ Завершить настройку", "finish_setup_" + str(channel_id))]
        ]
        await event.respond(
            f"Telegram канал {channel_id} добавлен!\n"
            "Теперь настроим фильтры:\n"
            "• Слова для совпадения - сообщение будет сохранено, если содержит хотя бы одно из этих слов\n"
            "• Слова для исключения - сообщение будет пропущено, если содержит любое из этих слов\n\n"
            "❗️ Бот начнет парсить сообщения из этого канала через 4 минуты",
            buttons=buttons
        )
        return True
    except Exception as e:
        await event.respond(f"Ошибка при добавлении канала: {str(e)}")
        return False

async def get_channel_info(channel_id):
    try:
        client = TelegramClient('tg_info_session', config['api_id'], config['api_hash'])
        await client.start()
        
        try:
            channel = await client.get_entity(int(channel_id))
            return {
                'title': getattr(channel, 'title', 'Нет названия'),
                'username': getattr(channel, 'username', None)
            }
        except Exception as e:
            print(f"Ошибка при получении информации о канале {channel_id}: {str(e)}")
            return {
                'title': 'Недоступно',
                'username': None
            }
        finally:
            await client.disconnect()
    except Exception as e:
        print(f"Ошибка при подключении к Telegram: {str(e)}")
        return {
            'title': 'Ошибка',
            'username': None
        }

@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    user_id = event.sender_id
    username = event.sender.username
    
    if not user_exists(user_id):
        add_user(user_id, username)
    
    user = get_user(user_id)
    
    if is_admin(user_id):
        await update_admin_panel(event)
    else:
        if user and user['subscription_status']:
            subscription_end = datetime.strptime(user['subscription_end_date'], '%Y-%m-%d %H:%M:%S')
            days_left = (subscription_end - datetime.now()).days
            
            welcome_text = f"""
✨ **Добро пожаловать в бота для поиска заказов!**

🔍 **Ваш статус: Активная подписка**
⏳ До окончания подписки: {days_left} {'день' if days_left == 1 else 'дня' if 1 < days_left < 5 else 'дней'}

📊 **Текущие настройки:**
• Получение заказов: {'✅ Включено' if user['orders_enabled'] else '❌ Выключено'}
• Telegram: {'✅' if user['tg'] else '❌'}
• VK: {'✅' if user['vk'] else '❌'}
• Сайты: {'✅' if user['site'] else '❌'}

💡 Скоро будут доступны дополнительные источники!
Используйте кнопки ниже для управления настройками.
"""
            buttons = [
                [Button.inline("ℹ️ О сервисе", b"about_service")],
                [Button.inline("💎 Подписка", b"subscription_info")],
                [Button.inline(
                    "🔔 Выключить заказы" if user['orders_enabled'] else "🔕 Включить заказы", 
                    b"toggle_orders"
                )],
                [Button.inline("🎯 Фильтры источников", b"source_filters")]
            ]
            await event.respond(welcome_text, buttons=buttons)
        else:
            welcome_text = """
👋 **Добро пожаловать в бота для поиска заказов!**

🔍 **Что умеет этот бот:**
• Мониторинг заказов из разных источников
• Мгновенные уведомления о новых заказах
• Удобный просмотр деталей заказа

💎 **Преимущества подписки:**
• Доступ ко всем источникам заказов
• Приоритетные уведомления
• Сохранение истории заказов

📱 **Доступные источники с подпиской:**
• Telegram каналы
• VK группы и каналы
• Тематические сайты (скоро)

💰 **Тарифы:**
• 1 неделя - 190₽ (2$)
• 1 месяц - 590₽ (6$)
• 2 месяца - 990₽ (10$)
• 3 месяца - 1490₽ (15$)
• 6 месяцев - 2990₽ (30$)
• 1 год - 4990₽ (50$)

❗️ **Важно:** Сейчас у вас нет активной подписки. 
Для получения полного доступа к функционалу бота необходимо приобрести подписку.

"""
            buttons = [[Button.inline("💎 Купить подписку", b"buy_subscription")]]
            await event.respond(welcome_text, buttons=buttons)

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    user_id = event.sender_id
    data = event.data.decode()

    user = get_user(user_id)
    
    if not user:
        await event.answer("Ошибка: пользователь не найден")
        return

    if user['subscription_status']:
        if data == "about_service":
            about_text = """
ℹ️ **О сервисе**

🤖 Этот бот помогает находить заказы из разных источников:
• Telegram каналы
• VK группы и каналы
• Тематические сайты

📨 **Как это работает:**
1. Бот мониторит все подключенные источники
2. При появлении нового заказа вы получаете уведомление
3. Можно настроить фильтры по источникам
4. Включать/выключать уведомления когда нужно

🎯 **Преимущества:**
• Мгновенные уведомления
• Удобный просмотр заказов
• Гибкая настройка источников
• Экономия времени на поиске заказов
"""
            await event.edit(about_text, buttons=[[Button.inline("◀️ Назад", b"back_to_main")]])
            
        elif data == "toggle_orders":
            success = toggle_orders(user_id)
            if success:
                user = get_user(user_id)
                await event.answer(
                    "✅ Уведомления о заказах включены" if user['orders_enabled'] else "❌ Уведомления о заказах выключены",
                    alert=True
                )
                subscription_end = datetime.strptime(user['subscription_end_date'], '%Y-%m-%d %H:%M:%S')
                days_left = (subscription_end - datetime.now()).days
                
                welcome_text = f"""
✨ **Добро пожаловать в бота для поиска заказов!**

🔍 **Ваш статус: Активная подписка**
⏳ До окончания подписки: {days_left} {'день' if days_left == 1 else 'дня' if 1 < days_left < 5 else 'дней'}

📊 **Текущие настройки:**
• Получение заказов: {'✅ Включено' if user['orders_enabled'] else '❌ Выключено'}
• Telegram: {'✅' if user['tg'] else '❌'}
• VK: {'✅' if user['vk'] else '❌'}
• Сайты: {'✅' if user['site'] else '❌'}

💡 Скоро будут доступны дополнительные источники!
"""
                buttons = [
                    [Button.inline("ℹ️ О сервисе", b"about_service")],
                    [Button.inline("💎 Подписка", b"subscription_info")],
                    [Button.inline(
                        "🔔 Выключить заказы" if user['orders_enabled'] else "🔕 Включить заказы", 
                        b"toggle_orders"
                    )],
                    [Button.inline("🎯 Фильтры источников", b"source_filters")]
                ]
                await event.edit(welcome_text, buttons=buttons)
            else:
                await event.answer("❌ Произошла ошибка при изменении настроек", alert=True)

        elif data == "source_filters":
            filters_text = f"""
🎯 **Настройка источников заказов**

Выберите, откуда вы хотите получать заказы:

📊 **Текущие настройки:**
• Telegram: {'✅' if user['tg'] else '❌'}
• VK: {'✅' if user['vk'] else '❌'}
• Сайты: {'✅' if user['site'] else '❌'}

💡 Скоро будут доступны дополнительные источники!
"""
            buttons = [
                [Button.inline("📱 Telegram", b"toggle_tg")],
                [Button.inline("💬 VK", b"toggle_vk")],
                [Button.inline("🌐 Сайты", b"toggle_site")],
                [Button.inline("◀️ Назад", b"back_to_main")]
            ]
            await event.edit(filters_text, buttons=buttons)

        elif data.startswith("toggle_"):
            source_type = data.split("_")[1]
            
            if source_type in ['tg', 'vk', 'site']:
                current_status = user[source_type]
                success = update_sources(user_id, source_type, not current_status)
                
                if success:
                    user = get_user(user_id)
                    filters_text = f"""
🎯 **Настройка источников заказов**

Выберите, откуда вы хотите получать заказы:

📊 **Текущие настройки:**
• Telegram: {'✅' if user['tg'] else '❌'}
• VK: {'✅' if user['vk'] else '❌'}
• Сайты: {'✅' if user['site'] else '❌'}

💡 Скоро будут доступны дополнительные источники!
"""
                    buttons = [
                        [Button.inline("📱 Telegram", b"toggle_tg")],
                        [Button.inline("💬 VK", b"toggle_vk")],
                        [Button.inline("🌐 Сайты", b"toggle_site")],
                        [Button.inline("◀️ Назад", b"back_to_main")]
                    ]
                    await event.edit(filters_text, buttons=buttons)
                else:
                    await event.answer("❌ Произошла ошибка при изменении настроек", alert=True)

        elif data == "back_to_main":
            subscription_end = datetime.strptime(user['subscription_end_date'], '%Y-%m-%d %H:%M:%S')
            days_left = (subscription_end - datetime.now()).days
            
            welcome_text = f"""
✨ **Добро пожаловать в бота для поиска заказов!**

🔍 **Ваш статус: Активная подписка**
⏳ До окончания подписки: {days_left} {'день' if days_left == 1 else 'дня' if 1 < days_left < 5 else 'дней'}

📊 **Текущие настройки:**
• Получение заказов: {'✅ Включено' if user['orders_enabled'] else '❌ Выключено'}
• Telegram: {'✅' if user['tg'] else '❌'}
• VK: {'✅' if user['vk'] else '❌'}
• Сайты: {'✅' if user['site'] else '❌'}

💡 Скоро будут доступны дополнительные источники!
"""
            buttons = [
                [Button.inline("ℹ️ О сервисе", b"about_service")],
                [Button.inline("💎 Подписка", b"subscription_info")],
                [Button.inline(
                    "🔔 Выключить заказы" if user['orders_enabled'] else "🔕 Включить заказы", 
                    b"toggle_orders"
                )],
                [Button.inline("🎯 Фильтры источников", b"source_filters")]
            ]
            await event.edit(welcome_text, buttons=buttons)
            return

        elif data == "subscription_info":
            subscription_end = datetime.strptime(user['subscription_end_date'], '%Y-%m-%d %H:%M:%S')
            days_left = (subscription_end - datetime.now()).days
            end_date_formatted = subscription_end.strftime('%d.%m.%Y %H:%M')
            
            subscription_text = f"""
💎 **Покупка подписки**

👉 **Тарифы:**
• 1 неделя - 190₽ (2$)
• 1 месяц - 590₽ (6$)
• 2 месяца - 990₽ (10$)
• 3 месяца - 1490₽ (15$)
• 6 месяцев - 2990₽ (30$)
• 1 год - 4990₽ (50$)

🏦 **Способы оплаты:**

💵 **Криптой:**
`TMtZdB2KN7pYwHvdU6U8zEJR5axZHVE9u1` - Tron TRC20 

💳 **Переводом на карту:**
`2200701786733433`
**(Диана Сергеевна С.) Т-банк**

🛠 После оплаты нужно прислать чек операции в чат менеджеру @VECmanager
⏱ Доступ будет выдан в течение часа

📅 **Информация о текущей подписке:**
• Дата окончания: {end_date_formatted}
• Осталось: {days_left} {'день' if days_left == 1 else 'дня' if 1 < days_left < 5 else 'дней'}
• Длительность подписки: {user['subscription_duration']} {'месяц' if user['subscription_duration'] == 1 else 'месяца' if 1 < user['subscription_duration'] < 5 else 'месяцев'}
"""
            await event.edit(subscription_text, buttons=[[Button.inline("◀️ Назад", b"back_to_main")]])
            return

        elif data == "back_to_main":
            subscription_end = datetime.strptime(user['subscription_end_date'], '%Y-%m-%d %H:%M:%S')
            days_left = (subscription_end - datetime.now()).days
            
            welcome_text = f"""
✨ **Добро пожаловать в бота для поиска заказов!**

🔍 **Ваш статус: Активная подписка**
⏳ До окончания подписки: {days_left} {'день' if days_left == 1 else 'дня' if 1 < days_left < 5 else 'дней'}

📊 **Текущие настройки:**
• Получение заказов: {'✅ Включено' if user['orders_enabled'] else '❌ Выключено'}
• Telegram: {'✅' if user['tg'] else '❌'}
• VK: {'✅' if user['vk'] else '❌'}
• Сайты: {'✅' if user['site'] else '❌'}

💡 Скоро будут доступны дополнительные источники!
"""
            buttons = [
                [Button.inline("ℹ️ О сервисе", b"about_service")],
                [Button.inline("💎 Подписка", b"subscription_info")],
                [Button.inline(
                    "🔔 Выключить заказы" if user['orders_enabled'] else "🔕 Включить заказы", 
                    b"toggle_orders"
                )],
                [Button.inline("🎯 Фильтры источников", b"source_filters")]
            ]
            await event.edit(welcome_text, buttons=buttons)
            return

    if data == "buy_subscription":
        subscription_text = """
💎 **Покупка подписки**

👉 **Тарифы:**
• 1 неделя - 190₽ (2$)
• 1 месяц - 590₽ (6$)
• 2 месяца - 990₽ (10$)
• 3 месяца - 1490₽ (15$)
• 6 месяцев - 2990₽ (30$)
• 1 год - 4990₽ (50$)

🏦 **Способы оплаты:**

💵 **Криптой:**
`TMtZdB2KN7pYwHvdU6U8zEJR5axZHVE9u1` - Tron TRC20 

💳 **Переводом на карту:**
`2200701786733433`
**(Диана Сергеевна С.) Т-банк**

🛠 После оплаты нужно прислать чек операции в чат менеджеру @VECmanager
⏱ Доступ будет выдан в течение часа
"""
        await event.edit(
            subscription_text,
            buttons=[[Button.inline("◀️ Назад", b"back_to_start")]]
        )
        return

    elif data == "back_to_start":
        welcome_text = """
👋 **Добро пожаловать в бота для поиска заказов!**

🔍 **Что умеет этот бот:**
• Мониторинг заказов из разных источников
• Мгновенные уведомления о новых заказах
• Удобный просмотр деталей заказа

💎 **Преимущества подписки:**
• Доступ ко всем источникам заказов
• Приоритетные уведомления
• Сохранение истории заказов

📱 **Доступные источники с подпиской:**
• Telegram каналы
• VK группы и каналы
• Тематические сайты (скоро)

💰 **Тарифы:**
• 1 неделя - 190₽ (2$)
• 1 месяц - 590₽ (6$)
• 2 месяца - 990₽ (10$)
• 3 месяца - 1490₽ (15$)
• 6 месяцев - 2990₽ (30$)
• 1 год - 4990₽ (50$)

❗️ **Важно:** Сейчас у вас нет активной подписки. 
Для получения полного доступа к функционалу бота необходимо приобрести подписку.

"""
        buttons = [[Button.inline("💎 Купить подписку", b"buy_subscription")]]
        await event.edit(welcome_text, buttons=buttons)
        return

    if not is_admin(user_id):
        await event.answer("У вас нет доступа к этой функции")
        return

    if data == "add_channel":
        bot.next_handler = "waiting_channel_id"
        await event.edit(
            "Отправьте ID Telegram канала в формате -100xxx...\n"
            "Его можно получить, переслав любое сообщение из канала боту @getmyid_bot",
            buttons=[[Button.inline("◀️ Назад", b"back_to_menu")]]
        )
        
    elif data == "back_to_menu":
        await update_admin_panel(event)
        bot.next_handler = None
        
    elif data == "remove_channel":
        telegram_channels = config['sources']['telegram']['channels']
        if not telegram_channels:
            await event.answer("❌ Список каналов пуст", alert=True)
            return
            
        buttons = []
        for channel_id in telegram_channels:
            info = await get_channel_info(channel_id)
            channel_title = f"📢 {info['title']}"
            if info['username']:
                channel_title += f" (@{info['username']})"
            channel_title += f"\nID: {channel_id}"
            
            buttons.append([Button.inline(channel_title, f"remove_{channel_id}")])
            
        buttons.append([Button.inline("◀️ Назад", b"back_to_menu")])
        await event.edit("Выберите Telegram канал для удаления:", buttons=buttons)
    
    elif data == "list_channels":
        telegram_channels = config['sources'].get('telegram', {}).get('channels', {})
        
        if not telegram_channels:
            await event.answer("❌ Список каналов пуст", alert=True)
            return
            
        channel_info = []
        for channel_id, settings in telegram_channels.items():
            info = await get_channel_info(channel_id)
            
            status = "✅ Активен" if settings['active'] else "❌ Отключен"
            includes = ", ".join(settings['include_filters']) or "нет"
            excludes = ", ".join(settings['exclude_filters']) or "нет"
            
            channel_title = f"📢 {info['title']}"
            if info['username']:
                channel_title += f" (@{info['username']})"
            
            channel_info.append(
                f"{channel_title}\n"
                f"ID: {channel_id}\n"
                f"Статус: {status}\n"
                f"Слова для совпадения: {includes}\n"
                f"Слова для исключения: {excludes}\n"
            )
        
        await event.edit(
            "Список подключенных Telegram каналов:\n\n" + "\n\n".join(channel_info),
            buttons=[[Button.inline("◀️ Назад", b"back_to_menu")]]
        )
    
    elif data.startswith("add_include_"):
        channel_id = data.split("_")[2]
        bot.next_handler = f"waiting_include_{channel_id}"
        await event.edit(
            "Введите слова для совпадения через запятую.\n"
            "Сообщения будут сохраняться только если содержат хотя бы одно из этих слов.\n\n"
            "Например: видео, монтаж, ролик",
            buttons=[[Button.inline("◀️ Назад", f"finish_setup_{channel_id}")]]
        )
    
    elif data.startswith("add_exclude_"):
        channel_id = data.split("_")[2]
        bot.next_handler = f"waiting_exclude_{channel_id}"
        await event.edit(
            "Введите слова для исключения через запятую.\n"
            "Сообщения, содержащие эти слова, будут игнорироваться.\n\n"
            "Например: резюме, ищу работу, вакансия",
            buttons=[[Button.inline("◀️ Назад", f"finish_setup_{channel_id}")]]
        )
    
    elif data.startswith("finish_setup_"):
        channel_id = data.split("_")[2]
        settings = config['sources']['telegram']['channels'][channel_id]
        includes = ", ".join(settings['include_filters']) or "нет"
        excludes = ", ".join(settings['exclude_filters']) or "нет"
        
        buttons = [
            [Button.inline("➕ Слова для совпадения", f"add_include_{channel_id}")],
            [Button.inline("➖ Слова для исключения", f"add_exclude_{channel_id}")],
            [Button.inline("✅ Завершить настройку", b"back_to_menu")]
        ]
        
        await event.edit(
            f"📝 Настройки канала {channel_id}:\n\n"
            f"• Слова для совпадения: {includes}\n"
            f"• Слова для исключения: {excludes}",
            buttons=buttons
        )
    
    elif data == "test_post":
        messages_folder = config['sources']['telegram']['messages_folder']
        if not os.path.exists(messages_folder):
            await event.edit(
                "❌ Нет сохраненных сообщений",
                buttons=[[Button.inline("◀️ Назад", b"back_to_menu")]]
            )
            return
        
        message_files = glob.glob(os.path.join(messages_folder, "*.json"))
        if not message_files:
            await event.edit(
                "❌ Нет сохраненных сообщений",
                buttons=[[Button.inline("◀️ Назад", b"back_to_menu")]]
            )
            return
        
        message_file = random.choice(message_files)
        
        try:
            with open(message_file, 'r', encoding='utf-8') as f:
                messages = json.load(f)
                
            if not messages:
                await event.edit(
                    "❌ Файл с сообщениями пуст",
                    buttons=[[Button.inline("◀️ Назад", b"back_to_menu")]]
                )
                return
                
            message = random.choice(messages)
            text = f"📝 Сообщение из канала {message['channel_id']}:\n\n{message['text']}"
            
            if message.get('media_path'):
                media_path = message['media_path']
                if os.path.exists(media_path):
                    await event.edit(text, file=media_path, buttons=[[Button.inline("◀️ Назад", b"back_to_menu")]])
                else:
                    await event.edit(
                        text + "\n\n⚠️ Медиафайл не найден",
                        buttons=[[Button.inline("◀️ Назад", b"back_to_menu")]]
                    )
            else:
                await event.edit(text, buttons=[[Button.inline("◀️ Назад", b"back_to_menu")]])
                
        except Exception as e:
            await event.edit(
                f"❌ Ошибка при чтении сообщения: {str(e)}",
                buttons=[[Button.inline("◀️ Назад", b"back_to_menu")]]
            )
    
    elif data == "run_parser":
        telegram_channels = config['sources'].get('telegram', {}).get('channels', {})
        if not telegram_channels:
            await event.answer("❌ Нет добавленных каналов", alert=True)
            return
            
        active_channels = [channel_id for channel_id, settings in telegram_channels.items() if settings['active']]
        if not active_channels:
            await event.answer("❌ Нет активных каналов", alert=True)
            return
        
        message = await run_parser()
        await event.answer(message, alert=True)
        await update_admin_panel(event)

    elif data == "stop_parser":
        if await stop_parser():
            await event.answer("✅ Парсер остановлен", alert=True)
            print("🛑 Парсер остановлен")
            await update_admin_panel(event)
        else:
            await event.answer("❌ Парсер не был запущен", alert=True)

    elif data == "restart_parser":
        await stop_parser()
        success, message = await run_parser()
        if success:
            await event.answer("✅ Парсер перезапущен", alert=True)
            await update_admin_panel(event)
        else:
            await event.answer("❌ Ошибка при перезапуске парсера", alert=True)

    elif data.startswith("remove_"):
        channel_id = data.split("_")[1]
        telegram_channels = config['sources']['telegram']['channels']
        if channel_id in telegram_channels:
            del telegram_channels[channel_id]
            await save_config()
            
            success, message = await cleanup_channel_data(int(channel_id))
            if success:
                await event.respond(f"Канал {channel_id} и все его данные удалены")
            else:
                await event.respond(f"Канал удален из списка, но возникла ошибка при удалении данных: {message}")
                
            buttons = await get_admin_buttons()
            await event.respond("Выберите действие:", buttons=buttons)
        else:
            await event.respond("Канал не найден")

    elif data == "grant_subscription":
        bot.next_handler = "waiting_subscription_ids"
        await event.edit(
            "Отправьте ID пользователя или несколько ID через запятую для выдачи подписки\n"
            "Например: 123456789 или 123456789, 987654321",
            buttons=[
                [Button.inline("1 неделя", b"sub_duration_0.25")],
                [Button.inline("1 месяц", b"sub_duration_1")],
                [Button.inline("2 месяца", b"sub_duration_2")],
                [Button.inline("3 месяца", b"sub_duration_3")],
                [Button.inline("6 месяцев", b"sub_duration_6")],
                [Button.inline("1 год", b"sub_duration_12")],
                [Button.inline("◀️ Назад", b"back_to_menu")]
            ]
        )

    elif data.startswith("sub_duration_"):
        duration = float(data.split("_")[-1])
        bot.subscription_duration = duration
        duration_text = "неделю" if duration == 0.25 else f"{'месяц' if duration == 1 else 'месяца' if 1 < duration < 5 else 'месяцев'}"
        bot.next_handler = "waiting_subscription_ids"
        await event.edit(
            f"Выбран срок подписки: {duration_text}\n\n"
            "Отправьте ID пользователя или несколько ID через запятую для выдачи подписки\n"
            "Например: 123456789 или 123456789, 987654321",
            buttons=[[Button.inline("◀️ Назад", b"back_to_menu")]]
        )

    elif bot.next_handler == "waiting_subscription_ids":
        try:
            if not hasattr(bot, 'subscription_duration'):
                await event.respond(
                    "❌ Не выбран срок подписки. Вернитесь назад и выберите срок.",
                    buttons=[[Button.inline("◀️ Назад", b"grant_subscription")]]
                )
                return

            user_ids = [int(id.strip()) for id in event.text.split(',')]
            success_ids = []
            failed_ids = []
            
            for user_id in user_ids:
                user = get_user(user_id)
                is_prolongation = user and user['subscription_status'] and user['subscription_end_date']
                
                if set_subscription(user_id, bot.subscription_duration):
                    updated_user = get_user(user_id)
                    success_ids.append(str(user_id))
                    
                    end_date = datetime.strptime(updated_user['subscription_end_date'], '%Y-%m-%d %H:%M:%S')
                    days_left = (end_date - datetime.now()).days
                    end_date_formatted = end_date.strftime('%d.%m.%Y')
                    
                    if is_prolongation:
                        notification_text = f"""
✨ **Ваша подписка успешно продлена!**

⏳ Подписка продлена на {get_duration_text(bot.subscription_duration)}
📅 Дата окончания: {end_date_formatted}
⌛️ Всего дней подписки: {days_left} {'день' if days_left == 1 else 'дня' if 1 < days_left < 5 else 'дней'}
🔄 Общая длительность подписки: {updated_user['subscription_duration']} {'месяц' if updated_user['subscription_duration'] == 1 else 'месяца' if 1 < updated_user['subscription_duration'] < 5 else 'месяцев'}

Для просмотра актуальной информации, напишите команду /start

Приятного использования! 🎉
"""
                    else:
                        notification_text = f"""
✨ **Спасибо за приобретение подписки!**

⏳ Срок подписки: {get_duration_text(bot.subscription_duration)}
📅 Дата окончания: {end_date_formatted}
⌛️ Дней до окончания: {days_left} {'день' if days_left == 1 else 'дня' if 1 < days_left < 5 else 'дней'}

Для начала работы с ботом, пожалуйста, напишите команду /start

Приятного использования! 🎉
"""
                    try:
                        await bot.send_message(user_id, notification_text)
                    except Exception as e:
                        print(f"Не удалось отправить уведомление пользователю {user_id}: {e}")
                else:
                    failed_ids.append(str(user_id))
            
            report = f"📊 Результат выдачи подписок на {get_duration_text(bot.subscription_duration)}:\n\n"
            
            if success_ids:
                report += "✅ Успешно выдано:\n"
                for user_id in success_ids:
                    user = get_user(int(user_id))
                    if user:
                        end_date = datetime.strptime(user['subscription_end_date'], '%Y-%m-%d %H:%M:%S')
                        days_left = (end_date - datetime.now()).days
                        end_date_formatted = end_date.strftime('%d.%m.%Y')
                        
                        username = f"@{user['username']}" if user['username'] else "без username"
                        
                        report += f"👤 {username} (ID: {user_id})\n"
                        report += f"   ⌛️ Подписка: {user['subscription_duration']} {'месяц' if user['subscription_duration'] == 1 else 'месяца' if 1 < user['subscription_duration'] < 5 else 'месяцев'}\n"
                        report += f"   📅 До: {end_date_formatted} ({days_left} {'день' if days_left == 1 else 'дня' if 1 < days_left < 5 else 'дней'})\n\n"
                
            if failed_ids:
                report += "❌ Не удалось выдать:\n"
                for user_id in failed_ids:
                    user = get_user(int(user_id))
                    username = f"@{user['username']}" if user and user['username'] else "без username"
                    report += f"• {username} (ID: {user_id})\n"
            
            delattr(bot, 'subscription_duration')
            bot.next_handler = None
            
            buttons = await get_admin_buttons()
            await event.respond(report, buttons=buttons)
            
        except ValueError:
            await event.respond(
                "❌ Неверный формат ID. Попробуйте еще раз или нажмите Назад",
                buttons=[[Button.inline("◀️ Назад", b"back_to_menu")]]
            )
        return

@bot.on(events.NewMessage)
async def message_handler(event):
    if not hasattr(bot, 'next_handler') or bot.next_handler is None:
        return
        
    if bot.next_handler == "waiting_channel_id":
        try:
            channel_id = int(event.text)
            telegram_channels = config['sources']['telegram']['channels']
            if str(channel_id) not in telegram_channels:
                await add_channel_with_filters(event, channel_id)
            else:
                await event.respond(
                    "Этот канал уже добавлен",
                    buttons=[[Button.inline("◀️ Назад", b"back_to_menu")]]
                )
        except ValueError:
            await event.respond(
                "Неверный формат ID канала. Попробуйте еще раз",
                buttons=[[Button.inline("◀️ Назад", b"back_to_menu")]]
            )
        bot.next_handler = None
    
    elif isinstance(bot.next_handler, str) and bot.next_handler.startswith("waiting_include_"):
        channel_id = bot.next_handler.split("_")[2]
        words = [word.strip().lower() for word in event.text.split(",") if word.strip()]
        config['sources']['telegram']['channels'][channel_id]['include_filters'] = words
        await save_config()
        
        settings = config['sources']['telegram']['channels'][channel_id]
        includes = ", ".join(settings['include_filters']) or "нет"
        excludes = ", ".join(settings['exclude_filters']) or "нет"
        
        buttons = [
            [Button.inline("➕ Слова для совпадения", f"add_include_{channel_id}")],
            [Button.inline("➖ Слова для исключения", f"add_exclude_{channel_id}")],
            [Button.inline("✅ Завершить настройку", b"back_to_menu")]
        ]
        
        await event.respond(
            f"📝 Настройки канала {channel_id}:\n\n"
            f"• Слова для совпадения: {includes}\n"
            f"• Слова для исключения: {excludes}",
            buttons=buttons
        )
        
        bot.next_handler = None
    
    elif isinstance(bot.next_handler, str) and bot.next_handler.startswith("waiting_exclude_"):
        channel_id = bot.next_handler.split("_")[2]
        words = [word.strip().lower() for word in event.text.split(",") if word.strip()]
        config['sources']['telegram']['channels'][channel_id]['exclude_filters'] = words
        await save_config()
        
        settings = config['sources']['telegram']['channels'][channel_id]
        includes = ", ".join(settings['include_filters']) or "нет"
        excludes = ", ".join(settings['exclude_filters']) or "нет"
        
        buttons = [
            [Button.inline("➕ Слова для совпадения", f"add_include_{channel_id}")],
            [Button.inline("➖ Слова для исключения", f"add_exclude_{channel_id}")],
            [Button.inline("✅ Завершить настройку", b"back_to_menu")]
        ]
        
        await event.respond(
            f"📝 Настройки канала {channel_id}:\n\n"
            f"• Слова для совпадения: {includes}\n"
            f"• Слова для исключения: {excludes}",
            buttons=buttons
        )
        
        bot.next_handler = None

    elif bot.next_handler == "waiting_subscription_ids":
        try:
            if not hasattr(bot, 'subscription_duration'):
                await event.respond(
                    "❌ Не выбран срок подписки. Вернитесь назад и выберите срок.",
                    buttons=[[Button.inline("◀️ Назад", b"grant_subscription")]]
                )
                return

            user_ids = [int(id.strip()) for id in event.text.split(',')]
            success_ids = []
            failed_ids = []
            
            for user_id in user_ids:
                user = get_user(user_id)
                is_prolongation = user and user['subscription_status'] and user['subscription_end_date']
                
                if set_subscription(user_id, bot.subscription_duration):
                    updated_user = get_user(user_id)
                    success_ids.append(str(user_id))
                    
                    end_date = datetime.strptime(updated_user['subscription_end_date'], '%Y-%m-%d %H:%M:%S')
                    days_left = (end_date - datetime.now()).days
                    end_date_formatted = end_date.strftime('%d.%m.%Y')
                    
                    if is_prolongation:
                        notification_text = f"""
✨ **Ваша подписка успешно продлена!**

⏳ Подписка продлена на {get_duration_text(bot.subscription_duration)}
📅 Дата окончания: {end_date_formatted}
⌛️ Всего дней подписки: {days_left} {'день' if days_left == 1 else 'дня' if 1 < days_left < 5 else 'дней'}
🔄 Общая длительность подписки: {updated_user['subscription_duration']} {'месяц' if updated_user['subscription_duration'] == 1 else 'месяца' if 1 < updated_user['subscription_duration'] < 5 else 'месяцев'}

Для просмотра актуальной информации, напишите команду /start

Приятного использования! 🎉
"""
                    else:
                        notification_text = f"""
✨ **Спасибо за приобретение подписки!**

⏳ Срок подписки: {get_duration_text(bot.subscription_duration)}
📅 Дата окончания: {end_date_formatted}
⌛️ Дней до окончания: {days_left} {'день' if days_left == 1 else 'дня' if 1 < days_left < 5 else 'дней'}

Для начала работы с ботом, пожалуйста, напишите команду /start

Приятного использования! 🎉
"""
                    try:
                        await bot.send_message(user_id, notification_text)
                    except Exception as e:
                        print(f"Не удалось отправить уведомление пользователю {user_id}: {e}")
                else:
                    failed_ids.append(str(user_id))
            
            # Формируем отчет для админа
            report = f"📊 Результат выдачи подписок на {get_duration_text(bot.subscription_duration)}:\n\n"
            
            if success_ids:
                report += "✅ Успешно выдано:\n"
                for user_id in success_ids:
                    user = get_user(int(user_id))
                    if user:
                        end_date = datetime.strptime(user['subscription_end_date'], '%Y-%m-%d %H:%M:%S')
                        days_left = (end_date - datetime.now()).days
                        end_date_formatted = end_date.strftime('%d.%m.%Y')
                        username = f"@{user['username']}" if user['username'] else "без username"
                        report += f"👤 {username} (ID: {user_id})\n"
                        report += f"   ⌛️ Подписка: {user['subscription_duration']} {'месяц' if user['subscription_duration'] == 1 else 'месяца' if 1 < user['subscription_duration'] < 5 else 'месяцев'}\n"
                        report += f"   📅 До: {end_date_formatted} ({days_left} {'день' if days_left == 1 else 'дня' if 1 < days_left < 5 else 'дней'})\n\n"
                
            if failed_ids:
                report += "❌ Не удалось выдать:\n"
                for user_id in failed_ids:
                    user = get_user(int(user_id))
                    username = f"@{user['username']}" if user and user['username'] else "без username"
                    report += f"• {username} (ID: {user_id})\n"
            
            delattr(bot, 'subscription_duration')
            bot.next_handler = None
            
            buttons = await get_admin_buttons()
            await event.respond(report, buttons=buttons)
            
        except ValueError:
            await event.respond(
                "❌ Неверный формат ID. Попробуйте еще раз или нажмите Назад",
                buttons=[[Button.inline("◀️ Назад", b"back_to_menu")]]
            )
        return

@bot.on(events.NewMessage(pattern='/add_admin'))
async def add_admin_handler(event):
    user_id = event.sender_id
    if not is_admin(user_id):
        return
    
    try:
        command_args = event.message.text.split()
        if len(command_args) != 2:
            await event.respond("❌ Неверный формат команды!\n\nИспользуйте: `/add_admin ID`\nгде ID это идентификатор пользователя")
            return
        new_admin_id = int(command_args[1])
        
        if not user_exists(new_admin_id):
            await event.respond("❌ Пользователь не найден в базе данных")
            return
            
        if is_admin(new_admin_id):
            await event.respond("❌ Пользователь уже является администратором")
            return
            
        if set_admin(new_admin_id, True):
            await event.respond(f"✅ Пользователь {new_admin_id} успешно назначен администратором")
        else:
            await event.respond("❌ Произошла ошибка при назначении администратора")
            
    except ValueError:
        await event.respond("❌ Неверный формат ID!\n\nИспользуйте: `/add_admin ID`\nгде ID это идентификатор пользователя")
    except Exception as e:
        await event.respond(f"❌ Произошла ошибка: {str(e)}")

@bot.on(events.NewMessage(pattern='/remove_admin'))
async def remove_admin_handler(event):
    user_id = event.sender_id
    if not is_admin(user_id):
        return
    
    try:
        command_args = event.message.text.split()
        if len(command_args) != 2:
            await event.respond("❌ Неверный формат команды!\n\nИспользуйте: `/remove_admin ID`\nгде ID это идентификатор администратора")
            return
        admin_id = int(command_args[1])
        
        if not user_exists(admin_id):
            await event.respond("❌ Пользователь не найден в базе данных")
            return
            
        if not is_admin(admin_id):
            await event.respond("❌ Пользователь не является администратором")
            return
            
        if set_admin(admin_id, False):
            await event.respond(f"✅ Пользователь {admin_id} больше не является администратором")
        else:
            await event.respond("❌ Произошла ошибка при удалении администратора")
            
    except ValueError:
        await event.respond("❌ Неверный формат ID!\n\nИспользуйте: `/remove_admin ID`\nгде ID это идентификатор администратора")
    except Exception as e:
        await event.respond(f"❌ Произошла ошибка: {str(e)}")

async def update_admin_panel(event):
    global parser_task
    telegram_channels = config['sources'].get('telegram', {}).get('channels', {})
    channel_count = len(telegram_channels)
    parser_status = "✅ Активен" if parser_task and not parser_task.done() else "❌ Неактивен"
    
    last_run = "Нет данных"
    try:
        messages_folder = config['sources']['telegram']['messages_folder']
        message_files = [f for f in os.listdir(messages_folder) if f.endswith('.json')]
        if message_files:
            latest_file = max(message_files, key=lambda x: os.path.getctime(os.path.join(messages_folder, x)))
            last_run = datetime.fromtimestamp(os.path.getctime(os.path.join(messages_folder, latest_file))).strftime("%d.%m.%Y %H:%M")
    except Exception:
        pass

    panel_text = f"""
🎯 **Панель управления ботом**

🔍 **Основные функции:**
• Добавление и удаление каналов для парсинга
• Настройка фильтров для каждого канала
• Управление парсером
• Управление администраторами

📋 **Доступные команды:**
➕ *Добавить канал* - подключение нового канала
➖ *Удалить канал* - отключение канала от парсинга
📋 *Список каналов* - просмотр всех подключенных каналов
🔄 *Запустить парсер* - начать сбор сообщений

👨‍💼 **Управление администраторами и подписками:**
• `/add_admin ID` - назначить администратора
• `/remove_admin ID` - снять права администратора
• `/reset_subscription ID` - обнулить подписку пользователя
• `/stats` - просмотр статистики сообщений

💡 *Подсказка:* Для добавления канала вам понадобится его ID в формате -100xxx...
Его можно получить, переслав любое сообщение из канала боту @getmyid_bot

ℹ️ Текущая статистика:
• Каналов подключено: {channel_count}
• Последний запуск парсера: {last_run}
• Статус парсера: {parser_status}
"""

    buttons = await get_admin_buttons()

    try:
        if hasattr(event, 'message') and event.message:
            try:
                await event.message.delete()
            except Exception as e:
                print(f"Ошибка при удалении предыдущего сообщения: {e}")
    except Exception as e:
        print(f"Ошибка при проверке сообщения: {e}")

    await event.respond(panel_text, buttons=buttons)

async def cleanup_sent_messages(file_path, messages):
    try:
        for message in messages:
            if message.get('media_path') and os.path.exists(message['media_path']):
                try:
                    os.remove(message['media_path'])
                    print(f"✅ Удален медиафайл: {message['media_path']}")
                except Exception as e:
                    print(f"❌ Ошибка при удалении медиафайла {message['media_path']}: {str(e)}")
        
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"✅ Удален файл с сообщениями: {file_path}")
            
        print(f"✅ Очищены отправленные сообщения и медиафайлы")
        cleanup_old_messages(30)
        
    except Exception as e:
        print(f"❌ Ошибка при очистке сообщений: {str(e)}")

async def parser_loop():
    while is_running:
        try:
            if config['sources']['telegram'].get('enabled'):
                print("\n💬 Запуск Telegram парсера...")
                tg_process = await asyncio.create_subprocess_exec(
                    'python', '-u', 'tg_parser.py',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'}
                )
                
                while True:
                    try:
                        line = await tg_process.stdout.readline()
                        if not line:
                            break
                        print(line.decode('utf-8', errors='ignore').strip())
                    except Exception as e:
                        print(f"Ошибка при чтении вывода Telegram парсера: {str(e)}")
                        break
                
                await tg_process.wait()
                await process_new_messages('telegram')
            
            if config['sources']['vk'].get('enabled'):
                print("\n💬 Запуск VK парсера...")
                vk_process = await asyncio.create_subprocess_exec(
                    'python', '-u', 'vk_parser.py',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'}
                )
                
                while True:
                    try:
                        line = await vk_process.stdout.readline()
                        if not line:
                            break
                        print(line.decode('utf-8', errors='ignore').strip())
                    except Exception as e:
                        print(f"Ошибка при чтении вывода VK парсера: {str(e)}")
                        break
                
                await vk_process.wait()
                await process_new_messages('vk')
            
            if config['sources']['hh'].get('enabled'):
                print("\n💼 Запуск HH парсера...")
                hh_process = await asyncio.create_subprocess_exec(
                    'python', '-u', 'hh_parser.py',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'}
                )
                
                while True:
                    try:
                        line = await hh_process.stdout.readline()
                        if not line:
                            break
                        print(line.decode('utf-8', errors='ignore').strip())
                    except Exception as e:
                        print(f"Ошибка при чтении вывода HH парсера: {str(e)}")
                        break
                
                await hh_process.wait()
                await process_new_messages('hh')
            
            await asyncio.sleep(120)
            
        except Exception as e:
            print(f"Ошибка в цикле парсера: {str(e)}")
            await asyncio.sleep(60)

async def main():
    print("Бот запущен. Нажмите Ctrl+C для остановки")
    
    try:
        await bot.start(bot_token=config['bot_token'])
        await bot.run_until_disconnected()
    except KeyboardInterrupt:
        print("\nПолучен сигнал завершения...")
    except Exception as e:
        print(f"Произошла ошибка: {str(e)}")
    finally:
        if parser_process:
            kill_process_tree(parser_process.pid)
            parser_process = None
        await bot.disconnect()
        print("Бот остановлен")

@bot.on(events.NewMessage(pattern='/stats'))
async def stats_handler(event):
    user_id = event.sender_id
    if not is_admin(user_id):
        return
        
    try:
        stats = get_sent_messages_stats()
        stats_text = f"""
📊 **Статистика отправленных сообщений**

📈 Всего отправлено: {stats['total']}
🕒 За последние 24 часа: {stats['last_24h']}

📱 По источникам:"""

        for source, count in stats['by_source'].items():
            emoji = "📢" if source == "telegram" else "💬" if source == "vk" else "🌐"
            stats_text += f"\n{emoji} {source.title()}: {count}"
            
        await event.respond(stats_text)
            
    except Exception as e:
        await event.respond(f"❌ Ошибка при получении статистики: {str(e)}")

@bot.on(events.NewMessage(pattern='/reset_subscription'))
async def reset_subscription_handler(event):
    user_id = event.sender_id
    if not is_admin(user_id):
        return
    
    try:
        command_args = event.message.text.split()
        if len(command_args) != 2:
            await event.respond("❌ Неверный формат команды!\n\nИспользуйте: `/reset_subscription ID`\nгде ID это идентификатор пользователя")
            return
            
        target_user_id = int(command_args[1])
        if not user_exists(target_user_id):
            await event.respond("❌ Пользователь не найден в базе данных")
            return
            
        user = get_user(target_user_id)
        username = f"@{user['username']}" if user['username'] else "без username"
        
        if reset_subscription(target_user_id):
            await event.respond(f"✅ Подписка пользователя {username} (ID: {target_user_id}) успешно обнулена")
            
            try:
                notification_text = """
❗️ **Ваша подписка деактивирована**

Доступ к функциям бота ограничен.
Для возобновления работы необходимо приобрести новую подписку.

Используйте команду /start для получения информации о тарифах.
"""
                await bot.send_message(target_user_id, notification_text)
            except Exception as e:
                print(f"Не удалось отправить уведомление пользователю {target_user_id}: {e}")
        else:
            await event.respond("❌ Произошла ошибка при обнулении подписки")
            
    except ValueError:
        await event.respond("❌ Неверный формат ID!\n\nИспользуйте: `/reset_subscription ID`\nгде ID это идентификатор пользователя")
    except Exception as e:
        await event.respond(f"❌ Произошла ошибка: {str(e)}")

def get_duration_text(duration: float) -> str:
    if duration == 0.25:
        return "1 неделю"
    elif duration == 1:
        return "1 месяц"
    elif duration == 2:
        return "2 месяца"
    elif duration == 3:
        return "3 месяца"
    elif duration == 6:
        return "6 месяцев"
    elif duration == 12:
        return "1 год"
    else:
        return f"{duration} месяцев"

def cleanup_old_messages(days: int):
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        cutoff_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
        c.execute('DELETE FROM sent_messages WHERE sent_date < ?', (cutoff_date,))
        deleted_count = c.rowcount
        conn.commit()
        conn.close()
        
        if deleted_count > 0:
            print(f"✅ Удалено {deleted_count} старых сообщений из базы данных")
            
    except Exception as e:
        print(f"❌ Ошибка при очистке старых сообщений: {str(e)}")

if __name__ == '__main__':
    asyncio.run(main())