import logging
import json
import os
import uuid
import asyncio
import signal
import sys
from aiohttp import web, ClientTimeout
from datetime import datetime
from collections import defaultdict
import re
from typing import Dict, List, Tuple, Any, Optional

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, 
    InlineKeyboardMarkup, ReplyKeyboardRemove
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramConflictError

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Глобальная переменная для отслеживания состояния бота
bot_instance_running = False

# Инициализация бота с правильными параметрами
API_TOKEN = '8400306221:AAGk7HnyDytn8ymhqTqNWZI8KtxW6CChb-E'

# Создаем бота с правильными настройками
bot = Bot(
    token=API_TOKEN, 
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Глобальные хранилища данных
polls = {}  # {poll_id: {name, questions: [...]}}
poll_id_counter = 1
admin_polls = defaultdict(list)  # {admin_id: [poll_id1, poll_id2, ...]}
poll_results = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))  # {poll_id: {question_idx: {answer: count}}}
user_progress = {}  # {user_id: {poll_id: {current_question_idx: int, answers: List[str], message_ids: List[int]}}}
active_polls = {}  # {message_id: (poll_id, question_idx)} для удаления сообщений

# Блокировка для избежания конфликтов
polling_lock = asyncio.Lock()

# Обработчики сигналов для корректного завершения
def signal_handler(signum, frame):
    """Обработчик сигналов для graceful shutdown"""
    global bot_instance_running
    logger.info(f"Получен сигнал {signum}, завершаем работу...")
    bot_instance_running = False
    asyncio.create_task(shutdown())

async def shutdown():
    """Корректное завершение работы бота"""
    logger.info("Завершение работы бота...")
    await bot.session.close()
    save_data()
    logger.info("Бот успешно завершил работу")

# Состояния для создания опроса
class PollCreationStates(StatesGroup):
    awaiting_poll_name = State()
    awaiting_question_text = State()
    awaiting_answer_options = State()
    awaiting_next_action = State()
    awaiting_new_question = State()
    configuring_answers = State()

# Загрузка данных из файла
def load_data():
    global polls, admin_polls, poll_results, poll_id_counter
    
    try:
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

# Сохранение данных в файл
def save_data():
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

# Улучшенная функция для обработки конфликтов
async def safe_polling():
    """Безопасный запуск polling с обработкой конфликтов"""
    global bot_instance_running
    
    max_retries = 5
    base_delay = 2
    retry_count = 0
    
    while retry_count < max_retries and bot_instance_running:
        try:
            async with polling_lock:
                logger.info(f"Запуск polling (попытка {retry_count + 1})")
                await dp.start_polling(bot)
                break
                
        except TelegramConflictError as e:
            retry_count += 1
            logger.warning(f"Конфликт обнаружен (попытка {retry_count}): {e}")
            
            if retry_count >= max_retries:
                logger.error("Достигнуто максимальное количество попыток. Завершаем работу.")
                break
                
            delay = base_delay * (2 ** retry_count)
            logger.info(f"Ожидание {delay} секунд перед повторной попыткой...")
            await asyncio.sleep(delay)
            
        except TelegramRetryAfter as e:
            logger.warning(f"Telegram требует подождать: {e.retry_after} сек.")
            await asyncio.sleep(e.retry_after)
            
        except TelegramNetworkError as e:
            retry_count += 1
            logger.warning(f"Сетевая ошибка (попытка {retry_count}): {e}")
            
            if retry_count >= max_retries:
                logger.error("Достигнуто максимальное количество попыток. Завершаем работу.")
                break
                
            delay = base_delay * (2 ** retry_count)
            await asyncio.sleep(delay)
            
        except Exception as e:
            logger.error(f"Неожиданная ошибка: {e}")
            break

# Валидация названия опроса
def validate_poll_name(name: str) -> Tuple[bool, str]:
    if not name or not name.strip():
        return False, "Название опроса не может быть пустым"
    if len(name) > 100:
        return False, "Название опроса не может превышать 100 символов"
    return True, ""

# Валидация текста вопроса
def validate_question_text(text: str) -> Tuple[bool, str]:
    if not text or not text.strip():
        return False, "Текст вопроса не может быть пустым"
    if len(text) > 300:
        return False, "Текст вопроса не может превышать 300 символов"
    return True, ""

# Валидация вариантов ответов
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

# Команда старт
@dp.message(Command("start"))
async def cmd_start(message: Message):
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📝 Создать опрос", callback_data="create_poll")
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="📊 Результаты", callback_data="show_results")
    keyboard.adjust(1)
    
    await message.answer(
        "Привет! Я бот для создания и проведения опросов с логическими ветвлениями.\n\n"
        "Выберите действие:",
        reply_markup=keyboard.as_markup()
    )

# Команда отмены
@dp.message(Command("cancel"))
async def cmd_cancel_message(message: Message, state: FSMContext):
    await message.answer("Действие отменено")
    await state.clear()
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📝 Создать опрос", callback_data="create_poll")
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="📊 Результаты", callback_data="show_results")
    keyboard.adjust(1)
    
    await message.answer(
        "Главное меню. Выберите действие:",
        reply_markup=keyboard.as_markup()
    )

@dp.callback_query(lambda c: c.data == "cancel")
async def cmd_cancel_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Действие отменено")
    await state.clear()
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📝 Создать опрос", callback_data="create_poll")
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="📊 Результаты", callback_data="show_results")
    keyboard.adjust(1)
    
    await callback.message.answer(
        "Главное меню. Выберите действие:",
        reply_markup=keyboard.as_markup()
    )

# Главное меню
@dp.callback_query(lambda c: c.data == "main_menu")
async def main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📝 Создать опрос", callback_data="create_poll")
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="📊 Результаты", callback_data="show_results")
    keyboard.adjust(1)
    
    await callback.message.edit_text(
        "Главное меню. Выберите действие:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Начало создания опроса
@dp.callback_query(lambda c: c.data == "create_poll")
async def create_poll_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PollCreationStates.awaiting_poll_name)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    
    await callback.message.edit_text(
        "Введите название опроса:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Обработка названия опроса
@dp.message(PollCreationStates.awaiting_poll_name)
async def process_poll_name(message: Message, state: FSMContext):
    poll_name = message.text.strip()
    
    is_valid, error_msg = validate_poll_name(poll_name)
    if not is_valid:
        await message.answer(f"❌ {error_msg}. Попробуйте еще раз:")
        return
        
    await state.update_data(
        poll_name=poll_name,
        poll_data={
            'name': poll_name,
            'questions': []
        }
    )
    await state.set_state(PollCreationStates.awaiting_question_text)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    
    await message.answer(
        "Введите текст первого вопроса:",
        reply_markup=keyboard.as_markup()
    )

# Обработка текста вопроса
@dp.message(PollCreationStates.awaiting_question_text)
async def process_question_text(message: Message, state: FSMContext):
    question_text = message.text.strip()
    
    is_valid, error_msg = validate_question_text(question_text)
    if not is_valid:
        await message.answer(f"❌ {error_msg}. Попробуйте еще раз:")
        return
        
    data = await state.get_data()
    poll_data = data['poll_data']
    
    new_question = {
        'text': question_text,
        'answers': []
    }
    poll_data['questions'].append(new_question)
    
    await state.update_data(poll_data=poll_data)
    await state.set_state(PollCreationStates.awaiting_answer_options)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    
    await message.answer(
        "Введите варианты ответов через запятую (например: Да, Нет, Не знаю):",
        reply_markup=keyboard.as_markup()
    )

# Обработка вариантов ответов
@dp.message(PollCreationStates.awaiting_answer_options)
async def process_answer_options(message: Message, state: FSMContext):
    answers = [ans.strip() for ans in message.text.split(',') if ans.strip()]
    
    is_valid, error_msg = validate_answer_options(answers)
    if not is_valid:
        await message.answer(f"❌ {error_msg}. Попробуйте еще раз:")
        return
        
    data = await state.get_data()
    poll_data = data['poll_data']
    current_question = poll_data['questions'][-1]
    
    for ans in answers:
        current_question['answers'].append({
            'text': ans,
            'next_question': None
        })
    
    await state.update_data(poll_data=poll_data)
    await state.set_state(PollCreationStates.configuring_answers)
    await show_answer_configuration_menu(message, state)

# Показать меню конфигурации ответов
async def show_answer_configuration_menu(message: Message, state: FSMContext):
    data = await state.get_data()
    poll_data = data['poll_data']
    current_question = poll_data['questions'][-1]
    
    keyboard = InlineKeyboardBuilder()
    
    # Кнопки для настройки каждого ответа
    for i, ans in enumerate(current_question['answers']):
        status = "✅" if ans['next_question'] is not None else "❌"
        keyboard.button(
            text=f"{i+1}. {ans['text']} {status}",
            callback_data=f"config_answer_{i}"
        )
    
    keyboard.adjust(1)
    
    # Кнопки управления
    keyboard.row(
        InlineKeyboardButton(text="➕ Добавить еще вопрос", callback_data="add_another_question"),
        InlineKeyboardButton(text="✅ Завершить опрос", callback_data="finish_poll")
    )
    keyboard.row(
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
    )
    
    await message.answer(
        "Настройте действия для ответов:\n"
        "✅ - действие настроено\n"
        "❌ - требуется настройка\n\n"
        "Выберите ответ для настройки:",
        reply_markup=keyboard.as_markup()
    )

# Конфигурация ответа
@dp.callback_query(PollCreationStates.configuring_answers, lambda c: c.data.startswith("config_answer_"))
async def configure_answer(callback: CallbackQuery, state: FSMContext):
    ans_idx = int(callback.data.split('_')[-1])
    await state.update_data(current_answer_index=ans_idx)
    
    data = await state.get_data()
    poll_data = data['poll_data']
    current_question = poll_data['questions'][-1]
    selected_answer = current_question['answers'][ans_idx]
    
    keyboard = InlineKeyboardBuilder()
    
    # Если есть другие вопросы, предлагаем привязать к ним
    if len(poll_data['questions']) > 1:
        keyboard.button(
            text="🔗 Привязать к существующему вопросу",
            callback_data=f"link_existing_{ans_idx}"
        )
    
    keyboard.button(
        text="➕ Создать новый вопрос",
        callback_data=f"create_new_{ans_idx}"
    )
    keyboard.button(
        text="⏹️ Завершить опрос для этого ответа",
        callback_data=f"end_poll_{ans_idx}"
    )
    keyboard.button(
        text="🔙 Назад к настройке ответов",
        callback_data="back_to_config"
    )
    
    await callback.message.edit_text(
        f"Настройка ответа: *{selected_answer['text']}*\n\n"
        "Выберите действие при выбора этого ответа:",
        reply_markup=keyboard.as_markup(),
        parse_mode="Markdown"
    )
    await callback.answer()

# Назад к меню конфигурации
@dp.callback_query(lambda c: c.data == "back_to_config")
async def back_to_configuration(callback: CallbackQuery, state: FSMContext):
    await show_answer_configuration_menu(callback.message, state)
    await callback.answer()

# Привязка к существующему вопросу
@dp.callback_query(PollCreationStates.configuring_answers, lambda c: c.data.startswith("link_existing_"))
async def link_to_existing_question(callback: CallbackQuery, state: FSMContext):
    ans_idx = int(callback.data.split('_')[-1])
    data = await state.get_data()
    poll_data = data['poll_data']
    
    # Создаем клавиатуру с существующими вопросами
    keyboard = InlineKeyboardBuilder()
    for i, question in enumerate(poll_data['questions']):
        if i < len(poll_data['questions']) - 1:  # Все кроме текущего
            display_text = question['text'][:30] + "..." if len(question['text']) > 30 else question['text']
            keyboard.button(
                text=f"{i+1}. {display_text}",
                callback_data=f"select_question_{ans_idx}_{i}"
            )
    
    keyboard.adjust(1)
    keyboard.button(text="🔙 Назад", callback_data=f"config_answer_{ans_idx}")
    
    await callback.message.edit_text(
        "Выберите вопрос, к которому привязать этот ответ:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Выбор вопроса для привязки
@dp.callback_query(PollCreationStates.configuring_answers, lambda c: c.data.startswith("select_question_"))
async def select_question_for_link(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split('_')
    ans_idx = int(parts[3])
    question_idx = int(parts[4])
    
    data = await state.get_data()
    poll_data = data['poll_data']
    current_question = poll_data['questions'][-1]
    
    # Привязываем ответ к выбранному вопросу
    current_question['answers'][ans_idx]['next_question'] = question_idx
    
    await state.update_data(poll_data=poll_data)
    await callback.answer(f"Ответ привязан к вопросу {question_idx + 1}!")
    await show_answer_configuration_menu(callback.message, state)

# Создание нового вопроса для ответа
@dp.callback_query(PollCreationStates.configuring_answers, lambda c: c.data.startswith("create_new_"))
async def create_new_question_for_answer(callback: CallbackQuery, state: FSMContext):
    ans_idx = int(callback.data.split('_')[-1])
    await state.update_data(current_answer_index=ans_idx)
    await state.set_state(PollCreationStates.awaiting_new_question)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data=f"config_answer_{ans_idx}")
    
    await callback.message.edit_text(
        "Введите текст следующего вопроса:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Обработка нового вопроса
@dp.message(PollCreationStates.awaiting_new_question)
async def process_new_question(message: Message, state: FSMContext):
    new_question_text = message.text.strip()
    
    is_valid, error_msg = validate_question_text(new_question_text)
    if not is_valid:
        await message.answer(f"❌ {error_msg}. Попробуйте еще раз:")
        return
        
    data = await state.get_data()
    poll_data = data['poll_data']
    ans_idx = data['current_answer_index']
    
    # Создаем новый вопрос
    new_question = {
        'text': new_question_text,
        'answers': []
    }
    poll_data['questions'].append(new_question)
    
    # Привязываем ответ к новому вопросу
    current_question = poll_data['questions'][-2]
    current_question['answers'][ans_idx]['next_question'] = len(poll_data['questions']) - 1
    
    await state.update_data(poll_data=poll_data)
    await state.set_state(PollCreationStates.awaiting_answer_options)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    
    await message.answer(
        "Введите варианты ответов для нового вопроса через запятую:",
        reply_markup=keyboard.as_markup()
    )

# Завершение опроса для ответа
@dp.callback_query(PollCreationStates.configuring_answers, lambda c: c.data.startswith("end_poll_"))
async def end_poll_for_answer(callback: CallbackQuery, state: FSMContext):
    ans_idx = int(callback.data.split('_')[-1])
    data = await state.get_data()
    poll_data = data['poll_data']
    current_question = poll_data['questions'][-1]
    
    # Устанавливаем завершение опроса для ответа
    current_question['answers'][ans_idx]['next_question'] = None
    
    await state.update_data(poll_data=poll_data)
    await callback.answer("Действие для ответа установлено: завершение опроса")
    await show_answer_configuration_menu(callback.message, state)

# Добавление еще одного вопроса
@dp.callback_query(PollCreationStates.configuring_answers, lambda c: c.data == "add_another_question")
async def add_another_question(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PollCreationStates.awaiting_question_text)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    
    await callback.message.edit_text(
        "Введите текст следующего вопроса:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Завершение создания опроса
@dp.callback_query(PollCreationStates.configuring_answers, lambda c: c.data == "finish_poll")
async def finish_poll_creation(callback: CallbackQuery, state: FSMContext):
    await finalize_poll_creation(callback, state)

# Финализация создания опроса
async def finalize_poll_creation(callback: CallbackQuery, state: FSMContext):
    global poll_id_counter
    data = await state.get_data()
    poll_data = data['poll_data']
    
    # Проверяем, есть ли ненастроенные ответы
    unfinished_answers = []
    for q_idx, q in enumerate(poll_data['questions']):
        for ans in q['answers']:
            if ans['next_question'] is None:
                unfinished_answers.append((q_idx + 1, q['text'], ans['text']))
    
    warning = ""
    if unfinished_answers:
        warning = "\n\n⚠️ Внимание: следующие ответы не настроены и будут завершать опрос:\n"
        for q_idx, q_text, ans_text in unfinished_answers[:3]:
            warning += f"{q_idx}. '{ans_text}' в вопросе '{q_text}'\n"
        if len(unfinished_answers) > 3:
            warning += f"и еще {len(unfinished_answers) - 3} ответов..."
    
    poll_id = poll_id_counter
    poll_id_counter += 1
    
    polls[poll_id] = poll_data
    admin_id = callback.from_user.id
    admin_polls[admin_id].append(poll_id)
    
    await state.clear()
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    
    await callback.message.edit_text(
        f"✅ Опрос *'{poll_data['name']}'* успешно создан!\n"
        f"ID опроса: `{poll_id}`\n"
        f"Количество вопросов: {len(poll_data['questions'])}"
        f"{warning}\n\n"
        "Теперь вы можете запустить его в группе через меню 'Мои опросы'",
        parse_mode="Markdown",
        reply_markup=keyboard.as_markup()
    )
    
    save_data()

# Показать мои опросы
@dp.callback_query(lambda c: c.data == "my_polls")
async def my_polls(callback: CallbackQuery):
    admin_id = callback.from_user.id
    if not admin_polls[admin_id]:
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="📝 Создать опрос", callback_data="create_poll")
        keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
        
        await callback.message.edit_text(
            "У вас нет созданных опросов.",
            reply_markup=keyboard.as_markup()
        )
        return
    
    keyboard = InlineKeyboardBuilder()
    for poll_id in admin_polls[admin_id]:
        poll = polls[poll_id]
        display_name = poll['name'][:30] + "..." if len(poll['name']) > 30 else poll['name']
        keyboard.button(
            text=f"{display_name} (ID: {poll_id})",
            callback_data=f"view_poll_{poll_id}"
        )
    
    keyboard.adjust(1)
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    
    await callback.message.edit_text(
        "Ваши опросы:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Просмотр опроса
@dp.callback_query(lambda c: c.data.startswith("view_poll_"))
async def view_poll_details(callback: CallbackQuery):
    poll_id = int(callback.data.split('_')[-1])
    if poll_id not in polls:
        await callback.answer("Ошибка: опрос не найден")
        return
    
    poll = polls[poll_id]
    text = f"📋 Опрос: *{poll['name']}*\nID: `{poll_id}`\n\n"
    
    def build_structure(question_idx, level=0):
        nonlocal text
        question = poll['questions'][question_idx]
        indent = "  " * level
        
        text += f"{indent}❓ {question_idx + 1}. {question['text']}\n"
        
        for i, ans in enumerate(question['answers']):
            text += f"{indent}    ➡️ {ans['text']}"
            
            if ans['next_question'] is not None:
                text += " → вопрос " + str(ans['next_question'] + 1) + "\n"
                build_structure(ans['next_question'], level + 1)
            else:
                text += " → завершение опроса\n"
    
    build_structure(0)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🚀 Запустить опрос", callback_data=f"start_poll_{poll_id}")
    keyboard.button(text="📊 Посмотреть результаты", callback_data=f"results_{poll_id}")
    keyboard.button(text="✏️ Редактировать опрос", callback_data=f"edit_poll_{poll_id}")
    keyboard.button(text="🗑️ Удалить опрос", callback_data=f"delete_poll_{poll_id}")
    keyboard.button(text="🔙 Назад", callback_data="my_polls")
    
    if len(text) > 4000:
        parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for part in parts[:-1]:
            await callback.message.answer(part, parse_mode="Markdown")
        await callback.message.edit_text(
            parts[-1],
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
    else:
        await callback.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
    await callback.answer()

# Запуск опроса
@dp.callback_query(lambda c: c.data.startswith("start_poll_"))
async def start_poll(callback: CallbackQuery):
    poll_id = int(callback.data.split('_')[-1])
    if poll_id not in polls:
        await callback.answer("Ошибка: опрос не найден")
        return
    
    poll = polls[poll_id]
    first_question = poll['questions'][0]
    
    keyboard = InlineKeyboardBuilder()
    for i, ans in enumerate(first_question['answers']):
        keyboard.button(
            text=ans['text'],
            callback_data=f"poll_{poll_id}_q0_a{i}"
        )
    
    keyboard.adjust(1)
    
    # Добавляем кнопку отмены для администратора
    if callback.from_user.id in admin_polls and poll_id in admin_polls[callback.from_user.id]:
        keyboard.row(InlineKeyboardButton(
            text="❌ Отмена (только для админа)",
            callback_data=f"admin_cancel_{poll_id}"
        ))
    
    poll_message = await callback.message.answer(
        f"📊 *{poll['name']}*\n\n"
        f"1. {first_question['text']}",
        reply_markup=keyboard.as_markup(),
        parse_mode="Markdown"
    )
    
    # Сохраняем ID сообщения для пользователя
    user_id = callback.from_user.id
    if user_id not in user_progress:
        user_progress[user_id] = {}
    
    user_progress[user_id][poll_id] = {
        'current_question_idx': 0,
        'answers': [],
        'message_ids': [poll_message.message_id]
    }
    
    await callback.answer("Опрос начат!")

# Обработка ответов на опрос (УЛУЧШЕННАЯ ВЕРСИЯ)
@dp.callback_query(lambda c: c.data.startswith("poll_"))
async def handle_poll_answer(callback: CallbackQuery):
    try:
        parts = callback.data.split('_')
        if len(parts) < 4:
            await callback.answer("Неверный формат данных")
            return
            
        poll_id = int(parts[1])
        question_idx = int(parts[2][1:])
        answer_idx = int(parts[3][1:])
        
        if poll_id not in polls:
            await callback.answer("Ошибка: опрос не найден")
            return
        
        user_id = callback.from_user.id
        poll = polls[poll_id]
        question = poll['questions'][question_idx]
        answer = question['answers'][answer_idx]
        
        # Сохраняем результат
        poll_results[poll_id][question_idx][answer['text']] += 1
        
        # Обновляем прогресс пользователя
        if user_id not in user_progress:
            user_progress[user_id] = {}
        
        if poll_id not in user_progress[user_id]:
            user_progress[user_id][poll_id] = {
                'current_question_idx': question_idx,
                'answers': [],
                'message_ids': []
            }
        
        user_data = user_progress[user_id][poll_id]
        user_data['answers'].append({
            'question_idx': question_idx,
            'answer_text': answer['text'],
            'question_text': question['text']
        })
        
        # Отмечаем выбранный ответ в предыдущем сообщении (редактируем его)
        try:
            # Создаем текст с историей ответов
            history_text = f"📊 *{poll['name']}*\n\n"
            for i, ans_data in enumerate(user_data['answers']):
                history_text += f"{i+1}. {ans_data['question_text']}\n"
                history_text += f"   ✅ Ответ: {ans_data['answer_text']}\n\n"
            
            # Редактируем предыдущее сообщение, убирая кнопки
            if user_data['message_ids']:
                last_message_id = user_data['message_ids'][-1]
                await bot.edit_message_text(
                    chat_id=callback.message.chat.id,
                    message_id=last_message_id,
                    text=history_text,
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Ошибка редактирования сообщения: {e}")
        
        # Проверяем следующее действие
        if answer['next_question'] is not None:
            next_idx = answer['next_question']
            next_question = poll['questions'][next_idx]
            
            keyboard = InlineKeyboardBuilder()
            for i, ans in enumerate(next_question['answers']):
                keyboard.button(
                    text=ans['text'],
                    callback_data=f"poll_{poll_id}_q{next_idx}_a{i}"
                )
            keyboard.adjust(1)
            
            # Отправляем следующий вопрос как новое сообщение
            new_message = await callback.message.answer(
                f"{next_idx + 1}. {next_question['text']}",
                reply_markup=keyboard.as_markup()
            )
            
            # Сохраняем ID нового сообщения
            user_data['message_ids'].append(new_message.message_id)
            user_data['current_question_idx'] = next_idx
            
        else:
            # Завершение опроса
            completion_text = (
                f"📊 *{poll['name']}*\n\n"
                f"Опрос завершен! Спасибо за участие! 🙌\n\n"
                f"Ваши ответы:\n"
            )
            
            for i, ans_data in enumerate(user_data['answers']):
                completion_text += f"{i+1}. {ans_data['question_text']}\n"
                completion_text += f"   ✅ Ответ: {ans_data['answer_text']}\n\n"
            
            await callback.message.answer(completion_text, parse_mode="Markdown")
            
            # Очищаем прогресс пользователя для этого опроса
            user_data['current_question_idx'] = None
        
        # Сохраняем данные
        save_data()
        
    except Exception as e:
        logger.error(f"Ошибка обработки ответа: {e}")
        await callback.answer("Произошла ошибка. Попробуйте еще раз.")
    
    await callback.answer()

# Отмена опроса администратором
@dp.callback_query(lambda c: c.data.startswith("admin_cancel_"))
async def admin_cancel_poll(callback: CallbackQuery):
    poll_id = int(callback.data.split('_')[-1])
    admin_id = callback.from_user.id
    
    if poll_id not in admin_polls[admin_id]:
        await callback.answer("❌ Только администратор опроса может отменить его")
        return
    
    # Удаляем сообщения опроса у пользователя
    user_id = callback.from_user.id
    if user_id in user_progress and poll_id in user_progress[user_id]:
        message_ids = user_progress[user_id][poll_id]['message_ids']
        for msg_id in message_ids:
            try:
                await bot.delete_message(callback.message.chat.id, msg_id)
            except Exception as e:
                logger.error(f"Ошибка удаления сообщения: {e}")
        
        # Очищаем прогресс
        del user_progress[user_id][poll_id]
    
    await callback.message.answer("Опрос отменен администратором")
    await callback.answer()

# Показать результаты
@dp.callback_query(lambda c: c.data == "show_results")
async def show_results(callback: CallbackQuery):
    admin_id = callback.from_user.id
    if not admin_polls[admin_id]:
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="📝 Создать опрос", callback_data="create_poll")
        keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
        
        await callback.message.edit_text(
            "У вас нет созданных опросов.",
            reply_markup=keyboard.as_markup()
        )
        return
    
    keyboard = InlineKeyboardBuilder()
    for poll_id in admin_polls[admin_id]:
        poll = polls[poll_id]
        display_name = poll['name'][:30] + "..." if len(poll['name']) > 30 else poll['name']
        keyboard.button(
            text=f"{display_name} (ID: {poll_id})",
            callback_data=f"results_{poll_id}"
        )
    
    keyboard.adjust(1)
    keyboard.button(text="🔙 Назад", callback_data="main_menu")
    
    await callback.message.edit_text(
        "Выберите опрос для просмотра результатов:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# HTTP-сервер для Render.com
async def handle_health_check(request):
    return web.Response(text="Bot is running!")

async def start_http_server():
    app = web.Application()
    app.router.add_get('/health', handle_health_check)
    app.router.add_get('/', handle_health_check)
    
    port = int(os.environ.get('PORT', 5000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"HTTP-сервер запущен на порту {port}")
    return runner

async def main():
    global bot_instance_running
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("=== Запуск бота для создания опросов с логическими ветвлениями ===")
    
    load_data()
    
    try:
        http_runner = await start_http_server()
        bot_instance_running = True
        
        logger.info("Запуск безопасного polling бота...")
        await safe_polling()
        
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")
        raise
    finally:
        bot_instance_running = False
        await bot.session.close()
        logger.info("Бот остановлен")

if __name__ == '__main__':
    asyncio.run(main())
