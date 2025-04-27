import json
import asyncio
import os
from datetime import datetime
from typing import Optional, List, Dict
import vk_api
import requests
import time
import sqlite3

def load_config():
    config_file = 'config.json'
    if not os.path.exists(config_file):
        config_file = 'config.example.json'
        print(f"⚠️ Файл config.json не найден, используем {config_file}")
        print("⚠️ Создайте файл config.json на основе примера и заполните его вашими данными")
    
    with open(config_file, 'r', encoding='utf-8') as f:
        return json.load(f)

config = load_config()
vk_config = config['sources'].get('vk', {})

# Проверяем настройки VK API
if vk_config.get('service_token') == "YOUR_VK_SERVICE_TOKEN":
    print("❌ Ошибка: VK API токен не настроен. Отредактируйте config.json")
    exit(1)

for folder in [vk_config.get('data_folder'), vk_config.get('messages_folder'), vk_config.get('media_folder')]:
    if folder and not os.path.exists(folder):
        os.makedirs(folder)

class VKParser:
    def __init__(self, service_token: str):
        try:
            self.vk = vk_api.VkApi(token=service_token)
            self.api = self.vk.get_api()
            print("✅ VK API успешно инициализирован")
        except Exception as e:
            print(f"❌ Ошибка при инициализации VK API: {str(e)}")
            raise

    async def get_group_id(self, group_name: str) -> Optional[int]:
        try:
            group_name = group_name.lstrip('-')
            response = self.api.groups.getById(group_id=group_name)
            if response:
                return -response[0]['id']
            return None
        except Exception as e:
            print(f"❌ Ошибка при получении ID группы {group_name}: {str(e)}")
            return None

    async def get_saved_messages(self) -> set:
        saved_messages = set()
        try:
            conn = sqlite3.connect('users.db')
            c = conn.cursor()
            c.execute('SELECT message_id FROM sent_messages WHERE source = "vk"')
            for row in c.fetchall():
                msg_id = row[0]
                saved_messages.add(msg_id)
            conn.close()
            print(f"✅ Загружено {len(saved_messages)} сохраненных сообщений из базы данных")
        except Exception as e:
            print(f"❌ Ошибка при чтении сохраненных сообщений из базы данных: {str(e)}")
            
            try:
                for filename in os.listdir(vk_config['messages_folder']):
                    if filename.endswith('.json'):
                        file_path = os.path.join(vk_config['messages_folder'], filename)
                        with open(file_path, 'r', encoding='utf-8') as f:
                            messages = json.load(f)
                            for msg in messages:
                                msg_id = f"vk_{msg['owner_id']}_{msg['message_id']}"
                                saved_messages.add(msg_id)
                print(f"✅ Загружено {len(saved_messages)} сохраненных сообщений из файлов")
            except Exception as e:
                print(f"❌ Ошибка при чтении сохраненных сообщений из файлов: {str(e)}")
        return saved_messages

    def should_save_message(self, text: str, group_settings: Dict) -> bool:
        if not text:
            return False
            
        text = text.lower()
        
        for word in group_settings['exclude_filters']:
            if word.lower() in text:
                print(f"❌ Сообщение содержит исключающее слово '{word}', пропускаем")
                return False
        
        if group_settings['include_filters']:
            for word in group_settings['include_filters']:
                if word.lower() in text:
                    print(f"✅ Найдено совпадение по слову '{word}'")
                    return True
            print("❌ Не найдено совпадений по словам для включения, пропускаем")
            return False
        
        return True

    async def download_media(self, url: str, file_path: str) -> bool:
        max_retries = 3
        retry_delay = 1

        for attempt in range(max_retries):
            try:
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                
                with open(file_path, 'wb') as f:
                    f.write(response.content)
                return True
                
            except requests.exceptions.RequestException as e:
                print(f"❌ Попытка {attempt + 1}/{max_retries} скачать медиафайл не удалась: {str(e)}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2
                continue
                
        return False

    async def process_attachments(self, post: Dict, timestamp: str) -> Optional[Dict]:
        if 'attachments' not in post:
            return None

        for att in post['attachments']:
            try:
                if att['type'] == 'photo':
                    sizes = att['photo']['sizes']
                    max_size = max(sizes, key=lambda x: x['width'] * x['height'])
                    url = max_size['url']
                    
                    file_path = os.path.join(vk_config['media_folder'], f"photo_{timestamp}.jpg")
                    if await self.download_media(url, file_path):
                        return {
                            'media_type': 'photo',
                            'media_path': file_path
                        }
                        
                elif att['type'] == 'video':
                    return {
                        'media_type': 'video',
                        'media_path': None,
                        'video_info': {
                            'owner_id': att['video']['owner_id'],
                            'video_id': att['video']['id'],
                            'title': att['video']['title']
                        }
                    }
            except Exception as e:
                print(f"❌ Ошибка при обработке вложения типа {att['type']}: {str(e)}")
                continue

        return None

    async def get_last_messages(self) -> bool:
        messages_data = []
        saved_messages = await self.get_saved_messages()
        
        try:
            for group_name, settings in vk_config['groups'].items():
                if not settings.get('active', False):
                    print(f"ℹ️ Группа {group_name} неактивна, пропускаем")
                    continue
                    
                try:
                    group_id = await self.get_group_id(group_name)
                    if not group_id:
                        print(f"❌ Не удалось получить ID группы {group_name}, пропускаем")
                        continue

                    print(f"🔍 Проверяю группу {group_name} (ID: {group_id})...")
                    
                    posts = self.api.wall.get(owner_id=group_id, count=5)
                    print(f"✅ Получено {len(posts['items'])} постов из группы {group_name}")
                    
                    for post in posts['items']:
                        try:
                            msg_id = f"vk_{group_id}_{post['id']}"
                            
                            if msg_id in saved_messages:
                                print(f"✓ Сообщение {msg_id} уже сохранено, пропускаем")
                                continue
                            
                            if not self.should_save_message(post.get('text', ''), settings):
                                continue
                            
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            
                            media_info = await self.process_attachments(post, timestamp)
                            
                            message_info = {
                                'source': 'vk',
                                'owner_id': group_id,
                                'message_id': post['id'],
                                'date': datetime.fromtimestamp(post['date']).isoformat(),
                                'text': post.get('text', ''),
                                'likes': post.get('likes', {}).get('count', 0),
                                'reposts': post.get('reposts', {}).get('count', 0),
                                'views': post.get('views', {}).get('count', 0),
                                'media_type': media_info['media_type'] if media_info else None,
                                'media_path': media_info['media_path'] if media_info else None
                            }
                            
                            messages_data.append(message_info)
                            print(f"✅ Получено новое сообщение из группы {group_name}")
                            if message_info['media_path']:
                                print(f"📎 Медиафайл сохранен: {message_info['media_path']}")
                                
                        except Exception as e:
                            print(f"❌ Ошибка при обработке поста из группы {group_name}: {str(e)}")
                            continue
                            
                except Exception as e:
                    print(f"❌ Ошибка при получении постов из группы {group_name}: {str(e)}")
                    continue
                    
                await asyncio.sleep(0.5)
            
            if messages_data:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_file = os.path.join(vk_config['messages_folder'], f'messages_{timestamp}.json')
                
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(messages_data, f, ensure_ascii=False, indent=4)
                
                print(f"✅ Сохранено {len(messages_data)} новых сообщений в файл: {output_file}")
                return True
            else:
                print("ℹ️ Нет новых сообщений для сохранения")
                return False
                
        except Exception as e:
            print(f"❌ Произошла общая ошибка: {str(e)}")
            return False

async def main():
    try:
        if not vk_config.get('enabled', False):
            print("❌ Источник VK отключен в конфигурации")
            return False

        if not vk_config.get('service_token'):
            print("❌ Не указан service_token в конфигурации")
            return False

        print("\n🔄 Инициализация VK парсера...")
        parser = VKParser(vk_config['service_token'])
        print("✅ VK парсер инициализирован")
        
        print("\n🔍 Начинаю проверку групп...")
        success = await parser.get_last_messages()
        
        if success:
            print("\n✅ VK парсер успешно завершил работу")
        else:
            print("\n⚠️ VK парсер завершил работу без новых сообщений")
        return success
        
    except Exception as e:
        print(f"\n❌ Критическая ошибка в VK парсере: {str(e)}")
        return False

if __name__ == '__main__':
    success = asyncio.run(main())
    exit(0 if success else 1)