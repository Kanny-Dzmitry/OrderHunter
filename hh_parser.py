import requests
from bs4 import BeautifulSoup
import json
from datetime import datetime
import time
import re
import os
from typing import Optional, List, Dict
import sqlite3

def load_config():
    config_file = 'config.json'
    if not os.path.exists(config_file):
        config_file = 'config.example.json'
        print(f"⚠️ Файл config.json не найден, используем {config_file}")
        print("⚠️ Создайте файл config.json на основе примера и заполните его вашими данными")
    
    with open(config_file, 'r', encoding='utf-8') as f:
        return json.load(f)

class HHParser:
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json'
        }
        self.base_url = "https://api.hh.ru/vacancies"
        self.params = {
            'text': 'видеомонтажер',
            'area': '1',
            'per_page': 5,
            'order_by': 'publication_time',
            'period': 1
        }
        self.data_folder = "hh"
        self.messages_folder = os.path.join(self.data_folder, "messages")
        os.makedirs(self.messages_folder, exist_ok=True)
        self.max_pages = 2
        self.max_vacancies = 5

    async def get_saved_messages(self) -> set:
        saved_messages = set()
        try:
            conn = sqlite3.connect('users.db')
            c = conn.cursor()
            c.execute('SELECT message_id FROM sent_messages WHERE source = "hh"')
            for row in c.fetchall():
                vacancy_id = row[0].split('_')[-1]
                saved_messages.add(vacancy_id)
            conn.close()
            print(f"✅ Загружено {len(saved_messages)} сохраненных вакансий из базы данных")
        except Exception as e:
            print(f"❌ Ошибка при чтении сохраненных вакансий из базы данных: {str(e)}")
        return saved_messages

    def should_save_message(self, vacancy: Dict) -> bool:
        if not vacancy.get('name') and not vacancy.get('description'):
            return False
        text = f"{vacancy.get('name', '')} {vacancy.get('description', '')}".lower()
        config = load_config()
        include_filters = config.get('sources', {}).get('hh', {}).get('include_filters', [])
        exclude_filters = config.get('sources', {}).get('hh', {}).get('exclude_filters', [])
        for word in exclude_filters:
            if word.lower() in text:
                print(f"❌ Вакансия содержит исключающее слово '{word}', пропускаем")
                return False
        if include_filters:
            for word in include_filters:
                if word.lower() in text:
                    print(f"✅ Найдено совпадение по слову '{word}'")
                    return True
            print("❌ Не найдено совпадений по словам для включения, пропускаем")
            return False
        return True

    def parse_vacancy(self, vacancy: Dict) -> Optional[Dict]:
        try:
            return {
                'source': 'hh',
                'vacancy_id': vacancy['id'],
                'title': vacancy.get('name', 'Название не указано'),
                'salary': self._format_salary(vacancy.get('salary')),
                'company': vacancy.get('employer', {}).get('name', 'Компания не указана'),
                'description': vacancy.get('description', ''),
                'link': vacancy.get('alternate_url', ''),
                'date': datetime.now().isoformat()
            }
        except Exception as e:
            print(f"❌ Ошибка при парсинге вакансии: {e}")
            return None

    def _format_salary(self, salary_data: Optional[Dict]) -> str:
        if not salary_data:
            return "Зарплата не указана"
        from_salary = salary_data.get('from')
        to_salary = salary_data.get('to')
        currency = salary_data.get('currency', 'RUR')
        if currency == 'RUR':
            currency = 'RUB'
        if from_salary and to_salary:
            return f"от {from_salary} до {to_salary} {currency}"
        elif from_salary:
            return f"от {from_salary} {currency}"
        elif to_salary:
            return f"до {to_salary} {currency}"
        return "Зарплата не указана"

    async def run(self) -> bool:
        messages_data = []
        saved_messages = await self.get_saved_messages()
        consecutive_old_vacancies = 0
        max_old_vacancies = 3
        page = 0
        try:
            while page < self.max_pages:
                print(f"\n🔍 Получаем вакансии с HH.ru (страница {page + 1} из {self.max_pages})...")
                self.params['page'] = page
                response = requests.get(self.base_url, params=self.params, headers=self.headers)
                if response.status_code != 200:
                    print(f"❌ Ошибка получения данных: {response.status_code}")
                    break
                data = response.json()
                vacancies = data.get('items', [])
                if not vacancies:
                    print("ℹ️ Больше вакансий не найдено")
                    break
                print(f"📥 Получено {len(vacancies)} вакансий с HH.ru")
                found_new_on_page = False
                for vacancy in vacancies:
                    if len(messages_data) >= self.max_vacancies:
                        print(f"\n✋ Достигнут лимит в {self.max_vacancies} новых вакансий")
                        break
                    try:
                        vacancy_id = str(vacancy['id'])
                        if vacancy_id in saved_messages:
                            print(f"⏩ Вакансия {vacancy_id} уже обработана ранее, пропускаем")
                            consecutive_old_vacancies += 1
                            continue
                        consecutive_old_vacancies = 0
                        found_new_on_page = True
                        print(f"\n🔍 Обрабатываем новую вакансию {vacancy_id}")
                        vacancy_response = requests.get(f"{self.base_url}/{vacancy_id}", headers=self.headers)
                        if vacancy_response.status_code == 200:
                            full_vacancy = vacancy_response.json()
                            if not self.should_save_message(full_vacancy):
                                continue
                            vacancy_data = self.parse_vacancy(full_vacancy)
                            if vacancy_data:
                                messages_data.append(vacancy_data)
                                print(f"✅ Получена новая вакансия: {vacancy_data['title']} ({len(messages_data)}/{self.max_vacancies})")
                    except Exception as e:
                        print(f"❌ Ошибка при обработке вакансии {vacancy.get('id')}: {str(e)}")
                        continue
                    time.sleep(1)
                if len(messages_data) >= self.max_vacancies:
                    break
                if not found_new_on_page and consecutive_old_vacancies >= max_old_vacancies:
                    print(f"\n🔄 Найдено {consecutive_old_vacancies} последовательных старых вакансий. Завершаем поиск.")
                    break
                page += 1
                time.sleep(2)
            if messages_data:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_file = os.path.join(self.messages_folder, f'messages_{timestamp}.json')
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(messages_data, f, ensure_ascii=False, indent=4)
                print(f"\n✅ Сохранено {len(messages_data)} новых вакансий")
                return True
            else:
                print("\nℹ️ Нет новых вакансий для сохранения")
                return False
        except Exception as e:
            print(f"\n❌ Произошла ошибка: {str(e)}")
            return False

async def main():
    try:
        config = load_config()
        if not config.get('sources', {}).get('hh', {}).get('enabled', False):
            print("❌ Источник HH отключен в конфигурации")
            return False
        print("\n🔄 Инициализация HH парсера...")
        parser = HHParser()
        print("✅ HH парсер инициализирован")
        success = await parser.run()
        if success:
            print("\n✅ HH парсер успешно завершил работу")
        else:
            print("\n⚠️ HH парсер завершил работу без новых вакансий")
        return success
    except Exception as e:
        print(f"\n❌ Критическая ошибка в HH парсере: {str(e)}")
        return False

if __name__ == '__main__':
    import asyncio
    success = asyncio.run(main())
    exit(0 if success else 1)