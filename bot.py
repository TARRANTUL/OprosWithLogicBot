import logging
import json
import os
import asyncio
from datetime import datetime
from collections import defaultdict
from typing import Dict, List, Tuple, Any

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, 
    InlineKeyboardMarkup
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Настройка логирования для Render
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Получение токена из переменных окружения
API_TOKEN = os.getenv('API_TOKEN', '8400306221:AAGk7HnyDytn8ymhqTqNWZI8KtxW6CChb-E')

# Инициализация бота
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Глобальные хранилища данных
polls = {}
poll_id_counter = 1
admin_polls = defaultdict(list)
poll_results = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
user_progress = {}
user_message_map = {}

# Классы состояний (остаются без изменений)
class PollCreationStates(StatesGroup):
    awaiting_poll_name = State()
    awaiting_question_text = State()
    awaiting_answer_options = State()
    awaiting_next_action = State()
    awaiting_new_question = State()

class PollEditStates(StatesGroup):
    selecting_question = State()
    editing_question = State()
    editing_answers = State()

# Функции для работы с данными (адаптированные для Render)
def load_data():
    """Загрузка данных из файла"""
    global polls, admin_polls, poll_results, poll_id_counter
    
    try:
        # На Render используем относительный путь
        if os.path.exists('poll_data.json'):
            with open('poll_data.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                polls = data.get('polls', {})
                
                admin_polls_data = data.get('admin_polls', {})
                for admin_id, poll_ids in admin_polls_data.items():
                    admin_polls[int(admin_id)] = poll_ids
                
                poll_results_data = data.get('poll_results', {})
                for poll_id_str, questions in poll_results_data.items():
                    poll_id = int(poll_id_str)
                    for q_idx_str, answers in questions.items():
                        q_idx = int(q_idx_str)
                        for answer, count in answers.items():
                            poll_results[poll_id][q_idx][answer] = count
                
                poll_id_counter = data.get('poll_id_counter', 1)
                logger.info("Данные успешно загружены")
                
    except Exception as e:
        logger.error(f"Ошибка загрузки данных: {e}")

def save_data():
    """Сохранение данных в файл"""
    try:
        data = {
            'polls': polls,
            'admin_polls': {str(k): v for k, v in admin_polls.items()},
            'poll_results': {
                str(poll_id): {
                    str(q_idx): dict(answers) 
                    for q_idx, answers in questions.items()
                } 
                for poll_id, questions in poll_results.items()
            },
            'poll_id_counter': poll_id_counter
        }
        
        with open('poll_data.json', 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        logger.info("Данные успешно сохранены")
    except Exception as e:
        logger.error(f"Ошибка сохранения данных: {e}")

# Валидационные функции (без изменений)
def validate_poll_name(name: str) -> Tuple[bool, str]:
    if not name or not name.strip():
        return False, "Название опроса не может быть пустым"
    if len(name) > 100:
        return False, "Название опроса не может превышать 100 символов"
    return True, ""

def validate_question_text(text: str) -> Tuple[bool, str]:
    if not text or not text.strip():
        return False, "Текст вопроса не может быть пустым"
    if len(text) > 300:
        return False, "Текст вопроса не может превышать 300 символов"
    return True, ""

def validate_answer_options(options: List[str]) -> Tuple[bool, str]:
    if not options or len(options) == 0:
        return False, "Должен быть хотя бы один вариант ответа"
    if len(options) > 10:
        return False, "Не может быть более 10 вариантов ответа"
    for option in options:
        if not option.strip():
            return False, "Вариант ответа не может быть пустым"
        if len(option) > 50:
            return False, "Вариант ответа не может превышать 50 символов"
    return True, ""

# Остальные функции бота остаются БЕЗ ИЗМЕНЕНИЙ
# [Вставь сюда весь остальной код из предыдущей версии]
# Команды: start, help, cancel, main_menu, create_poll_start, и т.д.
# Все обработчики сообщений и callback'ов

# Только добавь эту функцию в конец файла:
async def main():
    """Главная функция для запуска бота"""
    # Загружаем данные при запуске
    load_data()
    
    # Удаляем веб-хук (на всякий случай)
    await bot.delete_webhook(drop_pending_updates=True)
    
    logger.info("Бот запущен на Render.com!")
    
    # Запускаем опрос обновлений
    await dp.start_polling(bot)

if __name__ == "__main__":
    # Запускаем бота с обработкой ошибок
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")
