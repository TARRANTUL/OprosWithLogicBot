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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

bot_instance_running = False

API_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8400306221:AAGk7HnyDytn8ymhqTqNWZI8KtxW6CChb-E')
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

polls = {}
poll_id_counter = 1
admin_polls = defaultdict(list)
poll_results = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
user_progress = {}

polling_lock = asyncio.Lock()

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

class PollCreationStates(StatesGroup):
    awaiting_poll_name = State()
    awaiting_poll_structure = State()

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

def validate_answer_text(text: str) -> Tuple[bool, str]:
    if not text or not text.strip():
        return False, "Текст ответа не может быть пустым"
    if len(text) > 50:
        return False, "Текст ответа не может превышать 50 символов"
    return True, ""

def parse_poll_structure_by_spaces(text: str) -> Tuple[bool, Optional[Dict], str]:
    try:
        lines = [line.rstrip() for line in text.split('\n') if line.strip()]
        if not lines:
            return False, None, "Структура опроса не может быть пустой"
        
        normalized_lines = []
        for i, line in enumerate(lines):
            space_count = 0
            for char in line:
                if char == ' ':
                    space_count += 1
                else:
                    break
            
            level = space_count
            content = line[space_count:]
            normalized_lines.append((level, content, i + 1))
        
        poll_data = {'questions': []}
        stack = []
        current_question = None
        
        for level, content, line_num in normalized_lines:
            is_question = content.endswith('?')
            
            if is_question:
                is_valid, error_msg = validate_question_text(content)
                if not is_valid:
                    return False, None, f"Строка {line_num}: {error_msg}"
                
                new_question = {
                    'text': content,
                    'answers': [],
                    'level': level
                }
                
                if level == 0:
                    if poll_data['questions']:
                        return False, None, f"Строка {line_num}: может быть только один корневой вопрос (без пробелов)"
                    poll_data['questions'].append(new_question)
                    current_question = new_question
                    stack = [(0, new_question)]
                else:
                    if not stack:
                        return False, None, f"Строка {line_num}: нет родительского вопроса"
                    
                    while stack and stack[-1][0] >= level:
                        stack.pop()
                    
                    if not stack:
                        return False, None, f"Строка {line_num}: не найден родительский вопрос"
                    
                    parent_level, parent_question = stack[-1]
                    if level != parent_level + 1:
                        return False, None, f"Строка {line_num}: неправильный уровень вложенности. Ожидался уровень {parent_level + 1}"
                    
                    if not parent_question['answers']:
                        return False, None, f"Строка {line_num}: у родительского вопроса нет ответов"
                    
                    last_answer = parent_question['answers'][-1]
                    last_answer['next_question'] = len(poll_data['questions'])
                    
                    poll_data['questions'].append(new_question)
                    current_question = new_question
                    stack.append((level, new_question))
                
            else:
                if not current_question:
                    return False, None, f"Строка {line_num}: ответ не может быть перед вопросом"
                
                is_valid, error_msg = validate_answer_text(content)
                if not is_valid:
                    return False, None, f"Строка {line_num}: {error_msg}"
                
                expected_level = current_question['level']
                if level != expected_level:
                    return False, None, f"Строка {line_num}: неправильный уровень ответа. Ожидался уровень {expected_level}"
                
                existing_answers = [ans['text'] for ans in current_question['answers']]
                if content in existing_answers:
                    return False, None, f"Строка {line_num}: ответ '{content}' уже существует в этом вопросе"
                
                answer_data = {
                    'text': content,
                    'next_question': None
                }
                current_question['answers'].append(answer_data)
        
        if not poll_data['questions']:
            return False, None, "Не найден ни один вопрос"
        
        for i, question in enumerate(poll_data['questions']):
            if not question['answers']:
                return False, None, f"Вопрос '{question['text']}' не имеет ответов"
        
        return True, poll_data, ""
    
    except Exception as e:
        return False, None, f"Ошибка разбора структуры: {str(e)}"

@dp.message(Command("start"))
async def cmd_start(message: Message):
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📝 Создать опрос", callback_data="create_poll")
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="📊 Результаты", callback_data="show_results")
    keyboard.adjust(2)
    
    await message.answer(
        "Привет! Я бот для создания и проведения опросов с логическими ветвлениями.\n\n"
        "Выберите действие:",
        reply_markup=keyboard.as_markup()
    )

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

@dp.message(PollCreationStates.awaiting_poll_name)
async def process_poll_name(message: Message, state: FSMContext):
    poll_name = message.text.strip()
    
    is_valid, error_msg = validate_poll_name(poll_name)
    if not is_valid:
        await message.answer(f"❌ {error_msg}. Попробуйте еще раз:")
        return
        
    await state.update_data(poll_name=poll_name)
    await state.set_state(PollCreationStates.awaiting_poll_structure)
    
    instruction = """📝 Теперь введите структуру опроса с использованием пробелов:

<b>Правила форматирования:</b>
• Вопросы определяются по знаку ? на конце
• Без пробелов - корневой вопрос и ответы первого уровня
• 1 пробел - вопросы и ответы второго уровня  
• 2 пробела - вопросы и ответы третьего уровня
• И так далее...

<b>Пример из вашего скриншота:</b>
<code>Возьмем ли фокусника?
Да
 Какого?
  Витю
  Сашу
Нет</code>

<b>Разбор примера:</b>
• "Возьмем ли фокусника?" - корневой вопрос (0 пробелов)
• "Да", "Нет" - ответы первого уровня (0 пробелов)
• " Какого?" - вопрос второго уровня (1 пробел, привязан к ответу "Да")
• "  Витю", "  Сашу" - ответы третьего уровня (2 пробела, привязаны к вопросу "Какого?")"""

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📋 Подробный пример", callback_data="show_detailed_example")
    keyboard.button(text="🔄 Попробовать с примером", callback_data="try_with_example")
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    keyboard.adjust(1)
    
    await message.answer(instruction, reply_markup=keyboard.as_markup())

@dp.callback_query(lambda c: c.data == "show_detailed_example")
async def show_detailed_example(callback: CallbackQuery):
    example = """<b>Подробный разбор формата:</b>

<code>Корневой вопрос? (0 пробелов)
Ответ1 (0 пробелов)
Ответ2 (0 пробелов)
 Вопрос2? (1 пробел - привязан к последнему ответу)
  Ответ2.1 (2 пробела)
  Ответ2.2 (2 пробела)
   Вопрос3? (3 пробела - привязан к Ответ2.1)
    Ответ3.1 (4 пробела)
Ответ3 (0 пробелов)</code>

<b>Ваш пример из скриншота:</b>
<code>Возьмем ли фокусника?
Да
 Какого?
  Витю
  Сашу
Нет</code>

<b>Объяснение:</b>
• "Возьмем ли фокусника?" - корневой вопрос (0 пробелов)
• "Да" - ответ первого уровня (0 пробелов)
• " Какого?" - вопрос второго уровня (1 пробел), привязан к ответу "Да"
• "  Витю", "  Сашу" - ответы второго уровня (2 пробела), привязаны к вопросу "Какого?"
• "Нет" - ответ первого уровня (0 пробелов)"""

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔄 Использовать этот пример", callback_data="use_this_example")
    keyboard.button(text="⬅️ Назад", callback_data="back_to_creation")
    
    await callback.message.edit_text(example, parse_mode="HTML", reply_markup=keyboard.as_markup())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "use_this_example")
async def use_this_example(callback: CallbackQuery, state: FSMContext):
    example_text = """Возьмем ли фокусника?
Да
 Какого?
  Витю
  Сашу
Нет"""

    await state.update_data(example_text=example_text)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✅ Использовать этот текст", callback_data="apply_example")
    keyboard.button(text="⬅️ Назад", callback_data="back_to_creation")
    
    await callback.message.edit_text(
        f"<b>Пример готов к использованию:</b>\n\n<code>{example_text}</code>\n\n"
        "Нажмите кнопку ниже чтобы использовать этот текст:",
        parse_mode="HTML",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "apply_example")
async def apply_example(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    example_text = data.get('example_text', '')
    
    success, poll_data, error_msg = parse_poll_structure_by_spaces(example_text)
    
    if not success:
        await callback.message.edit_text(
            f"❌ Ошибка в примере: {error_msg}\n\nПопробуйте другой формат:",
            reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_creation")).as_markup()
        )
        return
    
    poll_name = data['poll_name']
    poll_data['name'] = poll_name
    
    global poll_id_counter
    poll_id = poll_id_counter
    poll_id_counter += 1
    
    polls[poll_id] = poll_data
    admin_id = callback.from_user.id
    admin_polls[admin_id].append(poll_id)
    
    await state.clear()
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    
    structure_info = f"✅ Опрос <b>'{poll_name}'</b> создан!\n\n"
    structure_info += f"<b>ID опроса:</b> <code>{poll_id}</code>\n"
    structure_info += f"<b>Всего вопросов:</b> {len(poll_data['questions'])}"
    
    await callback.message.edit_text(structure_info, parse_mode="HTML", reply_markup=keyboard.as_markup())
    save_data()
    await callback.answer()

@dp.callback_query(lambda c: c.data == "try_with_example")
async def try_with_example(callback: CallbackQuery, state: FSMContext):
    example_text = """Нравится ли вам программирование?
Да
 На каком языке программируете?
  Python
  JavaScript
  Другой
Нет
Затрудняюсь ответить"""

    await state.update_data(example_text=example_text)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📝 Редактировать пример", callback_data="edit_example")
    keyboard.button(text="✅ Использовать как есть", callback_data="apply_example")
    keyboard.button(text="⬅️ Назад", callback_data="back_to_creation")
    
    await callback.message.edit_text(
        f"<b>Простой пример для начала:</b>\n\n<code>{example_text}</code>\n\n"
        "Вы можете использовать его как есть или отредактировать:",
        parse_mode="HTML",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "back_to_creation")
async def back_to_creation(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    poll_name = data.get('poll_name', '')
    
    instruction = f"Продолжаем создание опроса: <b>{poll_name}</b>\n\n"
    instruction += "Введите структуру опроса с пробелами:"
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📋 Подробный пример", callback_data="show_detailed_example")
    keyboard.button(text="🔄 Попробовать с примером", callback_data="try_with_example")
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    keyboard.adjust(1)
    
    await callback.message.edit_text(instruction, parse_mode="HTML", reply_markup=keyboard.as_markup())
    await callback.answer()

@dp.message(PollCreationStates.awaiting_poll_structure)
async def process_poll_structure(message: Message, state: FSMContext):
    structure_text = message.text
    
    success, poll_data, error_msg = parse_poll_structure_by_spaces(structure_text)
    
    if not success:
        help_text = f"❌ {error_msg}\n\n"
        
        if "уровен" in error_msg.lower():
            help_text += "<b>Помощь по уровням:</b>\n"
            help_text += "• Корневой вопрос - без пробелов\n"
            help_text += "• Ответы на корневой вопрос - без пробелов\n"
            help_text += "• Вопросы второго уровня - 1 пробел\n"
            help_text += "• Ответы на вопросы второго уровня - 2 пробела\n"
            help_text += "• Вопросы третьего уровня - 3 пробела\n"
            help_text += "• И так далее...\n\n"
            help_text += "<b>Ваш пример должен выглядеть так:</b>\n"
            help_text += "<code>Возьмем ли фокусника?\nДа\n Какого?\n  Витю\n  Сашу\nНет</code>"
        
        help_text += "\n<b>Проверьте:</b>\n"
        help_text += "1. Корневой вопрос без пробелов и с '?' в конце\n"
        help_text += "2. Ответы на корневой вопрос без пробелов\n"
        help_text += "3. Вложенные вопросы с соответствующими пробелами"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="📋 Подробный пример", callback_data="show_detailed_example")
        keyboard.button(text="🔄 Попробовать с примером", callback_data="try_with_example")
        keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
        keyboard.button(text="❌ Отмена", callback_data="cancel")
        keyboard.adjust(1)
        
        await message.answer(help_text, parse_mode="HTML", reply_markup=keyboard.as_markup())
        return
    
    data = await state.get_data()
    poll_name = data['poll_name']
    poll_data['name'] = poll_name
    
    global poll_id_counter
    poll_id = poll_id_counter
    poll_id_counter += 1
    
    polls[poll_id] = poll_data
    admin_id = message.from_user.id
    admin_polls[admin_id].append(poll_id)
    
    await state.clear()
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    
    structure_info = f"✅ Опрос <b>'{poll_name}'</b> создан!\n\n"
    structure_info += f"<b>ID опроса:</b> <code>{poll_id}</code>\n"
    structure_info += f"<b>Всего вопросов:</b> {len(poll_data['questions'])}"
    
    await message.answer(structure_info, parse_mode="HTML", reply_markup=keyboard.as_markup())
    save_data()

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
