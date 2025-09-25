import logging
import json
import os
import asyncio
import signal
import sys
from aiohttp import web
from collections import defaultdict
from typing import Dict, List, Tuple, Any, Optional

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramConflictError

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Глобальная переменная для отслеживания состояния бота
bot_instance_running = False

# Инициализация бота
API_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8400306221:AAGk7HnyDytn8ymhqTqNWZI8KtxW6CChb-E')
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Глобальные хранилища данных
polls = {}
poll_id_counter = 1
admin_polls = defaultdict(list)
poll_results = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
user_progress = {}

# Блокировка для избежания конфликтов
polling_lock = asyncio.Lock()

# Обработчики сигналов для корректного завершения
def signal_handler(signum, frame):
    global bot_instance_running
    logger.info(f"Получен сигнал {signum}, завершаем работу...")
    bot_instance_running = False
    asyncio.create_task(shutdown())

async def shutdown():
    logger.info("Завершение работы бота...")
    await bot.session.close()
    save_data()
    logger.info("Бот успешно завершил работу")

# Состояния для создания опроса
class PollCreationStates(StatesGroup):
    awaiting_poll_name = State()
    awaiting_poll_structure = State()

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

# Валидация
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

# Парсер структуры опроса
def parse_poll_structure(text: str) -> Tuple[bool, Optional[Dict], str]:
    """
    Парсит структуру опроса из текста
    Возвращает: (успех, данные_опроса, сообщение_об_ошибке)
    """
    try:
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        if not lines:
            return False, None, "Структура опроса не может быть пустой"
        
        poll_data = {'questions': []}
        question_stack = []  # Стек для отслеживания текущего уровня вопросов
        current_question = None
        
        for i, line in enumerate(lines):
            line_num = i + 1
            
            # Определяем уровень вопроса/ответа
            if line.startswith('?'):
                # Это вопрос
                level = 0
                temp_line = line
                while temp_line.startswith('?'):
                    level += 1
                    temp_line = temp_line[1:].lstrip()
                
                # Извлекаем текст вопроса (в кавычках)
                if not (temp_line.startswith('"') and temp_line.endswith('"')):
                    return False, None, f"Строка {line_num}: текст вопроса должен быть в кавычках"
                
                question_text = temp_line[1:-1].strip()
                if not question_text:
                    return False, None, f"Строка {line_num}: текст вопроса не может быть пустым"
                
                # Валидация текста вопроса
                is_valid, error_msg = validate_question_text(question_text)
                if not is_valid:
                    return False, None, f"Строка {line_num}: {error_msg}"
                
                # Создаем новый вопрос
                new_question = {
                    'text': question_text,
                    'answers': [],
                    'level': level
                }
                
                if level == 1:
                    # Корневой вопрос
                    if poll_data['questions']:
                        return False, None, f"Строка {line_num}: может быть только один корневой вопрос (уровень 1)"
                    poll_data['questions'].append(new_question)
                    current_question = new_question
                    question_stack = [new_question]
                else:
                    # Подвопрос - должен следовать после ответа
                    if level > len(question_stack) + 1:
                        return False, None, f"Строка {line_num}: пропущены промежуточные уровни"
                    
                    # Находим родительский вопрос
                    while question_stack and question_stack[-1]['level'] >= level:
                        question_stack.pop()
                    
                    if not question_stack:
                        return False, None, f"Строка {line_num}: не найден родительский вопрос"
                    
                    parent_question = question_stack[-1]
                    if not parent_question['answers']:
                        return False, None, f"Строка {line_num}: у родительского вопроса нет ответов"
                    
                    # Привязываем к последнему ответу родительского вопроса
                    parent_answer = parent_question['answers'][-1]
                    parent_answer['next_question'] = len(poll_data['questions'])
                    
                    poll_data['questions'].append(new_question)
                    current_question = new_question
                    question_stack.append(new_question)
            
            elif line.startswith('-'):
                # Это ответ
                if not current_question:
                    return False, None, f"Строка {line_num}: ответ не может быть перед вопросом"
                
                level = 0
                temp_line = line
                while temp_line.startswith('-'):
                    level += 1
                    temp_line = temp_line[1:].lstrip()
                
                if level != current_question['level']:
                    return False, None, f"Строка {line_num}: уровень ответа ({level}) должен совпадать с уровнем вопроса ({current_question['level']})"
                
                # Извлекаем текст ответа (в кавычках)
                if not (temp_line.startswith('"') and temp_line.endswith('"')):
                    return False, None, f"Строка {line_num}: текст ответа должен быть в кавычках"
                
                answer_text = temp_line[1:-1].strip()
                if not answer_text:
                    return False, None, f"Строка {line_num}: текст ответа не может быть пустым"
                
                # Валидация текста ответа
                if len(answer_text) > 50:
                    return False, None, f"Строка {line_num}: текст ответа не может превышать 50 символов"
                
                # Проверяем дубликаты ответов в текущем вопросе
                existing_answers = [ans['text'] for ans in current_question['answers']]
                if answer_text in existing_answers:
                    return False, None, f"Строка {line_num}: ответ '{answer_text}' уже существует в этом вопросе"
                
                # Добавляем ответ к текущему вопросу
                answer_data = {
                    'text': answer_text,
                    'next_question': None  # По умолчанию - завершение опроса
                }
                current_question['answers'].append(answer_data)
            
            else:
                return False, None, f"Строка {line_num}: неверный формат. Используйте ? для вопросов и - для ответов"
        
        if not poll_data['questions']:
            return False, None, "Не найден ни один вопрос"
        
        # Проверяем, что у всех вопросов есть хотя бы один ответ
        for i, question in enumerate(poll_data['questions']):
            if not question['answers']:
                return False, None, f"Вопрос '{question['text']}' (уровень {question['level']}) не имеет ответов"
        
        return True, poll_data, ""
    
    except Exception as e:
        return False, None, f"Ошибка разбора структуры: {str(e)}"

# Команда старт
@dp.message(Command("start"))
async def cmd_start(message: Message):
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📝 Создать опрос", callback_data="create_poll")
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="📊 Результаты", callback_data="show_results")
    keyboard.adjust(2)  # 2 кнопки в строке для лучшего отображения
    
    await message.answer(
        "Привет! Я бот для создания и проведения опросов с логическими ветвлениями.\n\n"
        "Выберите действие:",
        reply_markup=keyboard.as_markup()
    )

# Команда отмены
@dp.message(Command("cancel"))
async def cmd_cancel_message(message: Message, state: FSMContext):
    await state.clear()
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📝 Создать опрос", callback_data="create_poll")
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="📊 Результаты", callback_data="show_results")
    keyboard.adjust(2)
    
    await message.answer(
        "Действие отменено. Главное меню:",
        reply_markup=keyboard.as_markup()
    )

@dp.callback_query(lambda c: c.data == "cancel")
async def cmd_cancel_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📝 Создать опрос", callback_data="create_poll")
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="📊 Результаты", callback_data="show_results")
    keyboard.adjust(2)
    
    await callback.message.edit_text(
        "Действие отменено. Главное меню:",
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
    keyboard.adjust(2)
    
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

# Обработка названия опроса и запрос структуры
@dp.message(PollCreationStates.awaiting_poll_name)
async def process_poll_name(message: Message, state: FSMContext):
    poll_name = message.text.strip()
    
    is_valid, error_msg = validate_poll_name(poll_name)
    if not is_valid:
        await message.answer(f"❌ {error_msg}. Попробуйте еще раз:")
        return
        
    await state.update_data(poll_name=poll_name)
    await state.set_state(PollCreationStates.awaiting_poll_structure)
    
    instruction = """📝 Теперь введите структуру опроса в формате:

? "Первый вопрос"
- "Ответ 1"
- "Ответ 2"
?? "Вопрос для Ответа 1"
-- "Ответ 1.1"
-- "Ответ 1.2"
??? "Вопрос для Ответа 1.2"
--- "Ответ 1.2.1"
- "Ответ 3"

<b>Правила:</b>
• <code>?</code> - уровень вопроса (1 знак = 1 уровень)
• <code>-</code> - уровень ответа (количество должно соответствовать уровню вопроса)
• Кавычки обязательны
• Каждый вопрос должен иметь хотя бы один ответ
• Первый вопрос должен быть уровня 1 (<code>?</code>)
• Максимум 10 уровней вложенности

<b>Пример:</b>
<code>? "Нравится ли вам программирование?"
- "Да"
- "Нет"
?? "На каком языке программируете?"
-- "Python"
-- "JavaScript"
-- "Другой"
??? "Какой именно?"
--- "Java"
--- "C++"
--- "Другой"
- "Затрудняюсь ответить"</code>"""

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    
    await message.answer(instruction, reply_markup=keyboard.as_markup())

# Обработка структуры опроса
@dp.message(PollCreationStates.awaiting_poll_structure)
async def process_poll_structure(message: Message, state: FSMContext):
    structure_text = message.text
    
    # Парсим структуру
    success, poll_data, error_msg = parse_poll_structure(structure_text)
    
    if not success:
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
        keyboard.button(text="❌ Отмена", callback_data="cancel")
        
        await message.answer(
            f"❌ Ошибка разбора структуры:\n{error_msg}\n\nПопробуйте еще раз:",
            reply_markup=keyboard.as_markup()
        )
        return
    
    # Получаем название опроса из состояния
    data = await state.get_data()
    poll_name = data['poll_name']
    poll_data['name'] = poll_name
    
    # Сохраняем опрос
    global poll_id_counter
    poll_id = poll_id_counter
    poll_id_counter += 1
    
    polls[poll_id] = poll_data
    admin_id = message.from_user.id
    admin_polls[admin_id].append(poll_id)
    
    await state.clear()
    
    # Показываем результат
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    
    # Строим текстовое представление структуры для подтверждения
    structure_info = f"✅ Опрос <b>'{poll_name}'</b> создан!\n\n"
    structure_info += "<b>Структура опроса:</b>\n"
    
    def build_structure_text(question_idx, level=0):
        nonlocal structure_info
        question = poll_data['questions'][question_idx]
        indent = "  " * level
        
        structure_info += f"{indent}❓ <b>{question_idx + 1}.</b> {question['text']}\n"
        
        for i, answer in enumerate(question['answers']):
            arrow = "→ завершение"
            if answer['next_question'] is not None:
                arrow = f"→ вопрос {answer['next_question'] + 1}"
            
            structure_info += f"{indent}   • {answer['text']} <i>{arrow}</i>\n"
            
            if answer['next_question'] is not None:
                build_structure_text(answer['next_question'], level + 1)
    
    build_structure_text(0)
    
    structure_info += f"\n<b>ID опроса:</b> <code>{poll_id}</code>"
    structure_info += f"\n<b>Всего вопросов:</b> {len(poll_data['questions'])}"
    
    await message.answer(
        structure_info,
        parse_mode="HTML",
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
        display_name = poll['name'][:25] + "..." if len(poll['name']) > 25 else poll['name']
        keyboard.button(text=f"{display_name} (ID: {poll_id})", callback_data=f"view_poll_{poll_id}")
    
    keyboard.adjust(1)
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    
    await callback.message.edit_text("Ваши опросы:", reply_markup=keyboard.as_markup())
    await callback.answer()

# Просмотр опроса
@dp.callback_query(lambda c: c.data.startswith("view_poll_"))
async def view_poll_details(callback: CallbackQuery):
    poll_id = int(callback.data.split('_')[-1])
    
    if poll_id not in polls:
        await callback.answer("Ошибка: опрос не найден")
        return
    
    poll = polls[poll_id]
    text = f"📋 <b>{poll['name']}</b> (ID: {poll_id})\n\n"
    
    def build_structure(question_idx, level=0):
        nonlocal text
        question = poll['questions'][question_idx]
        indent = "  " * level
        
        text += f"{indent}❓ <b>{question_idx + 1}.</b> {question['text']}\n"
        
        for i, ans in enumerate(question['answers']):
            arrow = "→"
            if ans['next_question'] is not None:
                arrow = f"→ вопрос {ans['next_question'] + 1}"
            else:
                arrow = "→ завершение"
            
            text += f"{indent}   • {ans['text']} <i>{arrow}</i>\n"
            
            if ans['next_question'] is not None:
                build_structure(ans['next_question'], level + 1)
    
    build_structure(0)
    
    # Компактная клавиатура с 2 кнопками в строке
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🚀 Запустить", callback_data=f"start_poll_{poll_id}")
    keyboard.button(text="📊 Результаты", callback_data=f"results_{poll_id}")
    keyboard.button(text="🗑️ Удалить", callback_data=f"delete_poll_{poll_id}")
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.adjust(2)
    
    if len(text) > 4000:
        parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for part in parts[:-1]:
            await callback.message.answer(part, parse_mode="HTML")
        await callback.message.edit_text(parts[-1], parse_mode="HTML", reply_markup=keyboard.as_markup())
    else:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard.as_markup())
    
    await callback.answer()

# Удаление опроса
@dp.callback_query(lambda c: c.data.startswith("delete_poll_"))
async def delete_poll(callback: CallbackQuery):
    poll_id = int(callback.data.split('_')[-1])
    admin_id = callback.from_user.id
    
    if poll_id not in polls or poll_id not in admin_polls[admin_id]:
        await callback.answer("Ошибка: опрос не найден")
        return
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✅ Удалить", callback_data=f"confirm_delete_{poll_id}")
    keyboard.button(text="❌ Отмена", callback_data=f"view_poll_{poll_id}")
    
    await callback.message.edit_text(
        f"❓ Удалить опрос <b>'{polls[poll_id]['name']}'</b>?",
        parse_mode="HTML",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Подтверждение удаления
@dp.callback_query(lambda c: c.data.startswith("confirm_delete_"))
async def confirm_delete_poll(callback: CallbackQuery):
    poll_id = int(callback.data.split('_')[-1])
    admin_id = callback.from_user.id
    
    if poll_id not in polls or poll_id not in admin_polls[admin_id]:
        await callback.answer("Ошибка: опрос не найден")
        return
    
    poll_name = polls[poll_id]['name']
    
    del polls[poll_id]
    admin_polls[admin_id].remove(poll_id)
    
    if poll_id in poll_results:
        del poll_results[poll_id]
    
    save_data()
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    
    await callback.message.edit_text(
        f"✅ Опрос '{poll_name}' удален!",
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
        text = ans['text'][:20] + "..." if len(ans['text']) > 20 else ans['text']
        keyboard.button(text=text, callback_data=f"poll_{poll_id}_q0_a{i}")
    
    keyboard.adjust(1)
    
    # Кнопка отмены только для администратора
    if callback.from_user.id in admin_polls and poll_id in admin_polls[callback.from_user.id]:
        keyboard.row(InlineKeyboardButton(text="❌ Отмена (админ)", callback_data=f"admin_cancel_{poll_id}"))
    
    await callback.message.edit_text(
        f"📊 <b>{poll['name']}</b>\n\n1. {first_question['text']}",
        reply_markup=keyboard.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()

# Обработка ответов на опрос
@dp.callback_query(lambda c: c.data.startswith("poll_"))
async def handle_poll_answer(callback: CallbackQuery):
    try:
        parts = callback.data.split('_')
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
        
        # Сохраняем прогресс пользователя
        if user_id not in user_progress:
            user_progress[user_id] = {}
        
        if poll_id not in user_progress[user_id]:
            user_progress[user_id][poll_id] = {
                'answers': [],
                'current_question': question_idx
            }
        
        user_data = user_progress[user_id][poll_id]
        user_data['answers'].append({
            'question_idx': question_idx,
            'answer_text': answer['text'],
            'question_text': question['text']
        })
        
        # Создаем текст с историей ответов
        history_text = f"📊 <b>{poll['name']}</b>\n\n"
        for i, ans_data in enumerate(user_data['answers']):
            history_text += f"{i+1}. {ans_data['question_text']}\n"
            history_text += f"   ✅ {ans_data['answer_text']}\n\n"
        
        # Проверяем следующее действие
        if answer['next_question'] is not None:
            next_idx = answer['next_question']
            next_question = poll['questions'][next_idx]
            
            keyboard = InlineKeyboardBuilder()
            for i, ans in enumerate(next_question['answers']):
                text = ans['text'][:20] + "..." if len(ans['text']) > 20 else ans['text']
                keyboard.button(text=text, callback_data=f"poll_{poll_id}_q{next_idx}_a{i}")
            
            keyboard.adjust(1)
            
            # Обновляем историю и добавляем новый вопрос
            history_text += f"{len(user_data['answers']) + 1}. {next_question['text']}"
            
            await callback.message.edit_text(
                history_text,
                parse_mode="HTML",
                reply_markup=keyboard.as_markup()
            )
            
            user_data['current_question'] = next_idx
            
        else:
            # Завершение опроса
            completion_text = history_text + "✅ Опрос завершен! Спасибо за участие! 🙌"
            await callback.message.edit_text(completion_text, parse_mode="HTML")
            
            # Очищаем прогресс
            user_data['current_question'] = None
        
        save_data()
        
    except Exception as e:
        logger.error(f"Ошибка обработки ответа: {e}")
        await callback.answer("Произошла ошибка")
    
    await callback.answer()

# Отмена опроса администратором
@dp.callback_query(lambda c: c.data.startswith("admin_cancel_"))
async def admin_cancel_poll(callback: CallbackQuery):
    poll_id = int(callback.data.split('_')[-1])
    admin_id = callback.from_user.id
    
    if poll_id not in admin_polls[admin_id]:
        await callback.answer("❌ Только администратор может отменить")
        return
    
    await callback.message.edit_text("Опрос отменен администратором")
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
        display_name = poll['name'][:25] + "..." if len(poll['name']) > 25 else poll['name']
        keyboard.button(text=f"{display_name} (ID: {poll_id})", callback_data=f"results_{poll_id}")
    
    keyboard.adjust(1)
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    
    await callback.message.edit_text(
        "Выберите опрос для просмотра результатов:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Показать результаты конкретного опроса
@dp.callback_query(lambda c: c.data.startswith("results_"))
async def show_poll_results(callback: CallbackQuery):
    poll_id = int(callback.data.split('_')[-1])
    
    if poll_id not in polls:
        await callback.answer("Ошибка: опрос не найден")
        return
    
    poll = polls[poll_id]
    results = poll_results[poll_id]
    
    report = f"📊 Результаты: <b>{poll['name']}</b>\n\n"
    total_participants = 0
    
    if results:
        first_question_results = results.get(0, {})
        total_participants = sum(first_question_results.values())
        report += f"👥 Участников: {total_participants}\n\n"
    
    def build_report(question_idx, level=0):
        nonlocal report
        question = poll['questions'][question_idx]
        indent = "  " * level
        
        report += f"{indent}❓ <b>{question_idx + 1}.</b> {question['text']}\n"
        
        if question_idx in results:
            question_results = results[question_idx]
            total_votes = sum(question_results.values())
            
            for ans_text, count in question_results.items():
                percentage = (count / total_votes * 100) if total_votes > 0 else 0
                report += f"{indent}   • {ans_text} - {count} ({percentage:.1f}%)\n"
                
                for ans in question['answers']:
                    if ans['text'] == ans_text and ans['next_question'] is not None:
                        build_report(ans['next_question'], level + 1)
        else:
            report += f"{indent}   • Нет ответов\n"
        
        report += "\n"
    
    if results:
        build_report(0)
    else:
        report += "Пока нет результатов.\n"
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    
    if len(report) > 4000:
        parts = [report[i:i+4000] for i in range(0, len(report), 4000)]
        for part in parts[:-1]:
            await callback.message.answer(part, parse_mode="HTML")
        await callback.message.edit_text(parts[-1], parse_mode="HTML", reply_markup=keyboard.as_markup())
    else:
        await callback.message.edit_text(report, parse_mode="HTML", reply_markup=keyboard.as_markup())
    
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
    
    logger.info("=== Запуск бота для создания опросов ===")
    
    load_data()
    
    try:
        http_runner = await start_http_server()
        bot_instance_running = True
        
        logger.info("Запуск безопасного polling...")
        await safe_polling()
        
    except Exception as e:
        logger.error(f"Ошибка при запуске: {e}")
        raise
    finally:
        bot_instance_running = False
        await bot.session.close()
        logger.info("Бот остановлен")

if __name__ == '__main__':
    asyncio.run(main())
