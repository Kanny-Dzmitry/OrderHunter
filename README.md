# OrderHunter

<div align="center">

![Python](https://img.shields.io/badge/Python-3.8+-green)
![License](https://img.shields.io/badge/license-MIT-blue)
![Version](https://img.shields.io/badge/version-1.0.0-orange)

</div>

<div align="center">
<h3>🌟 Автоматический бот для поиска заказов на видеомонтаж</h3>
</div>

Этот проект представляет собой мощный инструмент для автоматического сбора и обработки информации из различных источников: ВКонтакте, Telegram и HeadHunter. Бот предназначен для мониторинга и агрегации контента с возможностью гибкой фильтрации и управления.

## 🚀 Установка

1. Клонируйте репозиторий:
```bash
git clone https://github.com/yourusername/orderhunter.git
cd orderhunter
```

2. Создайте и активируйте виртуальное окружение:
```bash
python -m venv venv
source venv/bin/activate  # Для Linux/MacOS
venv\Scripts\activate  # Для Windows
```

3. Установите зависимости:
```bash
pip install -r requirements.txt
```

4. Настройте конфигурационный файл:
```bash
cp config.example.json config.json
```

## ⚙️ Настройка

### 1. Создание Telegram бота и получение API-ключей

1. Получите `api_id` и `api_hash` на [my.telegram.org](https://my.telegram.org)
2. Создайте бота через [@BotFather](https://t.me/BotFather)
3. Сохраните полученный токен бота

### 2. Получение VK токена

1. Создайте приложение на [vk.com/dev](https://vk.com/dev)
2. Получите `service_token` и `app_id`

### 3. Настройка конфигурационного файла

Откройте файл `config.json` и заполните следующие поля:
- `api_id`: Ваш Telegram API ID
- `api_hash`: Ваш Telegram API Hash
- `bot_token`: Токен вашего Telegram бота
- `sources.vk.service_token`: Ваш сервисный токен VK
- `sources.vk.app_id`: ID вашего приложения VK

Также настройте списки каналов Telegram и групп ВКонтакте, которые вы хотите мониторить, и добавьте соответствующие фильтры для отбора сообщений.

## 🔧 Использование

Запуск бота:
```bash
python bot.py
```

### Команды для пользователей

| Команда      | Описание              |
| ------------ | --------------------- |
| `/start`     | Начало работы с ботом |
| `/help`      | Получение справки     |
| `/profile`   | Просмотр профиля      |
| `/subscribe` | Управление подпиской  |
| `/settings`  | Настройки фильтров    |

### Команды для администраторов

| Команда         | Описание                  |
| --------------- | ------------------------- |
| `/admin`        | Панель администратора     |
| `/stats`        | Детальная статистика      |
| `/add_admin`    | Добавление администратора |
| `/remove_admin` | Удаление администратора   |
| `/broadcast`    | Рассылка сообщений        |

## 📋 Структура проекта

- `bot.py` - Основной файл бота, управляющий пользовательским интерфейсом
- `vk_parser.py` - Парсер для групп ВКонтакте
- `tg_parser.py` - Парсер для каналов Telegram
- `hh_parser.py` - Парсер для вакансий HeadHunter
- `database.py` - Работа с базой данных
- `config.json` - Конфигурационный файл (не включен в репозиторий)
- `config.example.json` - Пример конфигурационного файла

## 🤝 Вклад в проект

Contributions are welcome! Please feel free to submit a Pull Request.

## 📜 Лицензия

This project is licensed under the MIT License - see the LICENSE file for details.
