import json
import asyncio
import os
from telethon import TelegramClient
from datetime import datetime
from telethon.tl.types import InputPeerChannel, PeerChannel

def load_config():
    config_file = 'config.json'
    if not os.path.exists(config_file):
        config_file = 'config.example.json'
        print(f"⚠️ Файл config.json не найден, используем {config_file}")
        print("⚠️ Создайте файл config.json на основе примера и заполните его вашими данными")
    
    with open(config_file, 'r', encoding='utf-8') as f:
        return json.load(f)

config = load_config()
telegram_config = config['sources']['telegram']

# Проверяем настройки Telegram API
if config.get('api_id') == "YOUR_TELEGRAM_API_ID" or config.get('api_hash') == "YOUR_TELEGRAM_API_HASH":
    print("❌ Ошибка: Telegram API ключи не настроены. Отредактируйте config.json")
    exit(1)

for folder in [telegram_config['data_folder'], telegram_config['messages_folder'], telegram_config['media_folder']]:
    if not os.path.exists(folder):
        os.makedirs(folder)

async def resolve_channel(client, channel_id):
    try:
        channel = await client.get_input_entity(channel_id)
        return channel
    except ValueError:
        try:
            if str(channel_id).startswith('-100'):
                channel_id = int(str(channel_id)[4:])
            channel = await client.get_input_entity(PeerChannel(channel_id))
            return channel
        except Exception as e:
            print(f"Не удалось получить информацию о канале {channel_id}: {str(e)}")
            return None

async def get_saved_messages():
    saved_messages = set()
    try:
        for filename in os.listdir(telegram_config['messages_folder']):
            if filename.endswith('.json'):
                file_path = os.path.join(telegram_config['messages_folder'], filename)
                with open(file_path, 'r', encoding='utf-8') as f:
                    messages = json.load(f)
                    for msg in messages:
                        msg_id = f"tg_{msg['channel_id']}_{msg['message_id']}"
                        saved_messages.add(msg_id)
    except Exception as e:
        print(f"Ошибка при чтении сохраненных сообщений: {str(e)}")
    return saved_messages

def should_save_message(message, channel_settings):
    if not message.text:
        return False
        
    text = message.text.lower()
    
    for word in channel_settings['exclude_filters']:
        if word.lower() in text:
            print(f"❌ Сообщение содержит исключающее слово '{word}', пропускаем")
            return False
    
    if channel_settings['include_filters']:
        for word in channel_settings['include_filters']:
            if word.lower() in text:
                print(f"✅ Найдено совпадение по слову '{word}'")
                return True
        print("❌ Не найдено совпадений по словам для включения, пропускаем")
        return False
    
    return True

async def get_last_messages():
    messages_data = []
    
    try:
        config = load_config()
        telegram_config = config['sources']['telegram']
        
        if not telegram_config.get('enabled', False):
            print("❌ Источник Telegram отключен")
            return False
            
        channels = telegram_config.get('channels', {})
        
        if not channels:
            print("❌ Нет добавленных Telegram каналов")
            return False
            
        active_channels = [channel_id for channel_id, settings in channels.items() if settings['active']]
        if not active_channels:
            print("❌ Нет активных Telegram каналов")
            return False
        
        saved_messages = await get_saved_messages()
        
        print("🔄 Инициализация клиента...")
        client = TelegramClient('tg_parser_session', config['api_id'], config['api_hash'])
        
        try:
            print("🔄 Подключение к Telegram...")
            await client.start()
            
            if not await client.is_user_authorized():
                print("⚠️ Требуется авторизация.")
                print("Введите номер телефона в международном формате (например, +375291234567):")
                phone = input()
                await client.send_code_request(phone)
                print("Введите код подтверждения из Telegram:")
                code = input()
                await client.sign_in(phone, code)
                print("✅ Авторизация успешна!")
            
            print("✅ Подключение успешно")
            
        except Exception as e:
            print(f"❌ Ошибка при подключении к Telegram: {str(e)}")
            if hasattr(e, '__class__'):
                print(f"Тип ошибки: {e.__class__.__name__}")
            return False
        
        try:
            async with client:
                for channel_id, settings in channels.items():
                    if not settings['active']:
                        print(f"ℹ️ Telegram канал {channel_id} неактивен, пропускаем")
                        continue
                        
                    try:
                        print(f"🔍 Подключаемся к каналу {channel_id}...")
                        channel_id = int(channel_id)
                        channel = await resolve_channel(client, channel_id)
                        if not channel:
                            print(f"⚠️ Пропускаю Telegram канал {channel_id} - не удалось получить доступ")
                            continue
                            
                        print(f"🔍 Проверяю Telegram канал {channel_id}...")
                        
                        messages = await client.get_messages(channel, limit=1)
                        if not messages or len(messages) == 0:
                            print(f"ℹ️ В Telegram канале {channel_id} нет сообщений")
                            continue
                            
                        message = messages[0]
                        
                        msg_id = f"tg_{channel_id}_{message.id}"
                        if msg_id in saved_messages:
                            print(f"✓ Сообщение {msg_id} уже сохранено, пропускаем")
                            continue
                        
                        if not should_save_message(message, settings):
                            continue
                        
                        message_info = {
                            'source': 'telegram',
                            'channel_id': channel_id,
                            'message_id': message.id,
                            'date': message.date.isoformat(),
                            'text': message.text,
                            'views': message.views if hasattr(message, 'views') else None,
                            'media_type': None,
                            'media_path': None
                        }

                        if message.media:
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            if hasattr(message.media, 'photo'):
                                message_info['media_type'] = 'photo'
                                file_path = os.path.join(telegram_config['media_folder'], f"photo_{timestamp}.jpg")
                                await message.download_media(file_path)
                                message_info['media_path'] = file_path
                                
                            elif hasattr(message.media, 'document'):
                                for attribute in message.media.document.attributes:
                                    if hasattr(attribute, 'mime_type'):
                                        message_info['media_type'] = attribute.mime_type
                                    elif hasattr(attribute, 'animated'):
                                        message_info['media_type'] = 'gif'
                                
                                extension = '.mp4' if message_info['media_type'] == 'video' else '.gif'
                                file_path = os.path.join(telegram_config['media_folder'], f"media_{timestamp}{extension}")
                                await message.download_media(file_path)
                                message_info['media_path'] = file_path
                        
                        messages_data.append(message_info)
                        print(f"✅ Получено новое сообщение из Telegram канала {channel_id}")
                        if message_info['media_path']:
                            print(f"📎 Медиафайл сохранен: {message_info['media_path']}")
                        
                    except Exception as e:
                        print(f"❌ Ошибка при получении сообщения из Telegram канала {channel_id}: {str(e)}")
                        if hasattr(e, '__class__'):
                            print(f"Тип ошибки: {e.__class__.__name__}")
            
            if messages_data:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_file = os.path.join(telegram_config['messages_folder'], f'messages_{timestamp}.json')
                
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(messages_data, f, ensure_ascii=False, indent=4)
                
                print(f"✅ Новые сообщения сохранены в файл: {output_file}")
                return True
            else:
                print("ℹ️ Нет новых сообщений для сохранения")
                return False
        
        except Exception as e:
            print(f"❌ Произошла общая ошибка: {str(e)}")
            if hasattr(e, '__class__'):
                print(f"Тип ошибки: {e.__class__.__name__}")
            return False
        finally:
            await client.disconnect()
            
    except Exception as e:
        print(f"❌ Критическая ошибка: {str(e)}")
        if hasattr(e, '__class__'):
            print(f"Тип ошибки: {e.__class__.__name__}")
        return False

if __name__ == '__main__':
    success = asyncio.run(get_last_messages())
    exit(0 if success else 1)