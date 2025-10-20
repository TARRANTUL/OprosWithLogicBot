import logging
import json
import os
import asyncio
import signal
import sys
from typing import Dict, List, Tuple, Any, Optional
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, 
    CallbackQuery, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    Update
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Глобальные переменные
bot_instance_running = False

# Токен бота
API_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8400306221:AAGk7HnyDytn8ymhqTqNWZI8KtxW6CChb-E')

# Инициализация бота
bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Классы данных для структуры опроса
@dataclass
class Answer:
    text: str
    next_question: Optional[int] = None
    level: int = 0

@dataclass
class Question:
    text: str
    answers: List[Answer]
    level: int = 0

@dataclass
class Poll:
    name: str
    questions: List[Question]
    created_by: int
    created_at: str = field(default_factory=lambda: __import__('datetime').datetime.now().isoformat())

# Хранилище данных
class PollStorage:
    def __init__(self):
        self.polls: Dict[int, Poll] = {}
        self.poll_id_counter = 1
        self.admin_polls: Dict[int, List[int]] = defaultdict(list)
        self.poll_results: Dict[int, Dict[int, Dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        self.user_progress: Dict[int, Dict[int, Dict[str, Any]]] = {}
        self.active_polls: Dict[int, int] = {}  # {chat_id: poll_id}
    
    def add_poll(self, admin_id: int, poll: Poll) -> int:
        poll_id = self.poll_id_counter
        self.poll_id_counter += 1
        self.polls[poll_id] = poll
        self.admin_polls[admin_id].append(poll_id)
        return poll_id
    
    def get_poll(self, poll_id: int) -> Optional[Poll]:
        return self.polls.get(poll_id)
    
    def record_answer(self, poll_id: int, question_idx: int, answer_text: str):
        self.poll_results[poll_id][question_idx][answer_text] += 1
    
    def save_to_file(self, filename: str = 'poll_data.json'):
        try:
            data = {
                'polls': {},
                'poll_id_counter': self.poll_id_counter,
                'admin_polls': {str(k): v for k, v in self.admin_polls.items()},
                'poll_results': {
                    str(poll_id): {
                        str(q_idx): dict(answers) 
                        for q_idx, answers in questions.items()
                    } 
                    for poll_id, questions in self.poll_results.items()
                }
            }
            
            # Преобразуем опросы в словари для сериализации
            for poll_id, poll in self.polls.items():
                data['polls'][str(poll_id)] = {
                    'name': poll.name,
                    'created_by': poll.created_by,
                    'created_at': poll.created_at,
                    'questions': [
                        {
                            'text': q.text,
                            'level': q.level,
                            'answers': [
                                {
                                    'text': a.text,
                                    'next_question': a.next_question,
                                    'level': a.level
                                }
                                for a in q.answers
                            ]
                        }
                        for q in poll.questions
                    ]
                }
            
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            logger.info("Данные успешно сохранены")
        except Exception as e:
            logger.error(f"Ошибка сохранения данных: {e}")
    
    def load_from_file(self, filename: str = 'poll_data.json'):
        try:
            if not os.path.exists(filename):
                logger.info("Файл данных не найден, создаем пустое хранилище")
                return
            
            with open(filename, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            self.poll_id_counter = data.get('poll_id_counter', 1)
            
            # Загружаем админов и их опросы
            admin_polls_data = data.get('admin_polls', {})
            for admin_id, poll_ids in admin_polls_data.items():
                self.admin_polls[int(admin_id)] = poll_ids
            
            # Загружаем результаты опросов
            poll_results_data = data.get('poll_results', {})
            for poll_id_str, questions in poll_results_data.items():
                poll_id = int(poll_id_str)
                for q_idx_str, answers in questions.items():
                    q_idx = int(q_idx_str)
                    for answer, count in answers.items():
                        self.poll_results[poll_id][q_idx][answer] = count
            
            # Загружаем сами опросы
            polls_data = data.get('polls', {})
            for poll_id_str, poll_data in polls_data.items():
                poll_id = int(poll_id_str)
                
                questions = []
                for q_data in poll_data['questions']:
                    answers = [
                        Answer(
                            text=a_data['text'],
                            next_question=a_data['next_question'],
                            level=a_data['level']
                        )
                        for a_data in q_data['answers']
                    ]
                    
                    questions.append(
                        Question(
                            text=q_data['text'],
                            answers=answers,
                            level=q_data['level']
                        )
                    )
                
                poll = Poll(
                    name=poll_data['name'],
                    questions=questions,
                    created_by=poll_data['created_by'],
                    created_at=poll_data.get('created_at', __import__('datetime').datetime.now().isoformat())
                )
                
                self.polls[poll_id] = poll
            
            logger.info("Данные успешно загружены")
        except Exception as e:
            logger.error(f"Ошибка загрузки данных: {e}")

# Инициализируем хранилище
storage_manager = PollStorage()

class PollCreationStates(StatesGroup):
    awaiting_poll_name = State()
    awaiting_poll_structure = State()

def signal_handler(signum, frame):
    global bot_instance_running
    logger.info(f"Получен сигнал {signum}, завершаем работу...")
    bot_instance_running = False
    asyncio.create_task(shutdown())

async def shutdown():
    logger.info("Завершение работы бота...")
    await bot.session.close()
    storage_manager.save_to_file()
    logger.info("Бот успешно завершил работу")

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

def parse_poll_structure(text: str) -> Tuple[bool, Optional[Poll], str]:
    try:
        lines = [line.rstrip() for line in text.split('\n') if line.strip()]
        if not lines:
            return False, None, "Структура опроса не может быть пустой"
        
        # Нормализуем строки: определяем уровень вложенности
        normalized_lines = []
        for i, line in enumerate(lines):
            space_count = len(line) - len(line.lstrip(' '))
            level = space_count // 2  # 2 пробела = 1 уровень
            content = line.lstrip(' ')
            normalized_lines.append((level, content, i + 1))
        
        if not normalized_lines:
            return False, None, "Нет данных для обработки"
        
        if not normalized_lines[0][1].endswith('?'):
            return False, None, f"Строка {normalized_lines[0][2]}: первая строка должна быть вопросом (с ? в конце)"
        
        questions = []
        question_stack = []  # Стек для отслеживания вопросов и их уровней
        
        for level, content, line_num in normalized_lines:
            is_question = content.endswith('?')
            
            if is_question:
                is_valid, error_msg = validate_question_text(content)
                if not is_valid:
                    return False, None, f"Строка {line_num}: {error_msg}"
                
                new_question = Question(
                    text=content,
                    answers=[],
                    level=level
                )
                
                if level == 0:
                    if questions:
                        return False, None, f"Строка {line_num}: может быть только один корневой вопрос (без отступа)"
                    questions.append(new_question)
                    question_stack = [(0, len(questions) - 1)]
                else:
                    # Найти родительский вопрос
                    parent_idx = -1
                    for i in range(len(question_stack) - 1, -1, -1):
                        parent_level, parent_q_idx = question_stack[i]
                        if parent_level == level - 1:
                            parent_idx = parent_q_idx
                            break
                    
                    if parent_idx == -1:
                        return False, None, f"Строка {line_num}: неправильный уровень вложенности. Нет родительского вопроса для уровня {level}"
                    
                    # Найти последний ответ на родительский вопрос с уровнем level-1
                    parent_question = questions[parent_idx]
                    last_answer_idx = -1
                    for i in range(len(parent_question.answers) - 1, -1, -1):
                        if parent_question.answers[i].level == level - 1:
                            last_answer_idx = i
                            break
                    
                    if last_answer_idx == -1:
                        return False, None, f"Строка {line_num}: невозможно привязать вопрос к ответу - нет подходящего ответа на родительский вопрос"
                    
                    # Привязать вопрос к найденному ответу
                    parent_question.answers[last_answer_idx].next_question = len(questions)
                    questions.append(new_question)
                    question_stack = [(l, idx) for l, idx in question_stack if l < level] + [(level, len(questions) - 1)]
            
            else:
                is_valid, error_msg = validate_answer_text(content)
                if not is_valid:
                    return False, None, f"Строка {line_num}: {error_msg}"
                
                # Найти вопрос, которому принадлежит этот ответ
                parent_idx = -1
                for i in range(len(question_stack) - 1, -1, -1):
                    parent_level, parent_q_idx = question_stack[i]
                    if parent_level == level:
                        parent_idx = parent_q_idx
                        break
                
                if parent_idx == -1:
                    return False, None, f"Строка {line_num}: нет родительского вопроса для ответа '{content}'"
                
                parent_question = questions[parent_idx]
                
                # Проверить, что уровень ответа соответствует уровню вопроса
                if level != parent_question.level:
                    return False, None, f"Строка {line_num}: уровень ответа {level} не соответствует уровню вопроса {parent_question.level}"
                
                # Проверить уникальность ответа
                existing_answers = [ans.text for ans in parent_question.answers]
                if content in existing_answers:
                    return False, None, f"Строка {line_num}: ответ '{content}' уже существует в вопросе '{parent_question.text}'"
                
                answer_data = Answer(
                    text=content,
                    level=level
                )
                parent_question.answers.append(answer_data)
        
        if not questions:
            return False, None, "Не найден ни один вопрос"
        
        for i, question in enumerate(questions):
            if not question.answers:
                return False, None, f"Вопрос '{question.text}' не имеет ответов"
        
        return True, Poll(name="", questions=questions, created_by=0), ""
    
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

@dp.callback_query(F.data == "cancel")
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
    await callback.answer()

@dp.callback_query(F.data == "main_menu")
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

@dp.callback_query(F.data == "create_poll")
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
    
    instruction = """📝 Теперь введите структуру опроса с использованием отступов (по 2 пробела на уровень):

<b>Правила форматирования:</b>
• Вопросы определяются по знаку ? на конце
• Без отступа - корневой вопрос и ответы на него
• 2 пробела - вопросы второго уровня и ответы на них
• 4 пробела - вопросы третьего уровня и ответы на них

<b>Пример:</b>
<code>Возьмем ли фокусника?
Да
  Какого?
    Витю
    Сашу
Нет</code>

<b>Разбор примера:</b>
• "Возьмем ли фокусника?" - корневой вопрос (без отступа)
• "Да", "Нет" - ответы на корневой вопрос (без отступа)
• "  Какого?" - вопрос второго уровня (2 пробела), привязан к ответу "Да"
• "    Витю", "    Сашу" - ответы на вопрос второго уровня (4 пробела)"""

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📋 Подробный пример", callback_data="show_detailed_example")
    keyboard.button(text="🔄 Попробовать с примером", callback_data="try_with_example")
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    keyboard.adjust(1)
    
    await message.answer(instruction, reply_markup=keyboard.as_markup())

@dp.callback_query(F.data == "show_detailed_example")
async def show_detailed_example(callback: CallbackQuery):
    example = """<b>Подробный разбор формата:</b>

<code>Корневой вопрос? (без отступа)
Ответ1 (без отступа)
Ответ2 (без отступа)
  Вопрос2? (2 пробела - привязан к Ответ1)
    Ответ2.1 (4 пробела)
    Ответ2.2 (4 пробела)
  Вопрос3? (2 пробела - привязан к Ответ2)
    Ответ3.1 (4 пробела)</code>

<b>Ваш пример:</b>
<code>Возьмем ли фокусника?
Да
  Какого?
    Витю
    Сашу
Нет</code>

<b>Объяснение:</b>
• "Возьмем ли фокусника?" - корневой вопрос (без отступа)
• "Да" - ответ на корневой вопрос (без отступа)
• "  Какого?" - вопрос второго уровня (2 пробела), привязан к ответу "Да"
• "    Витю", "    Сашу" - ответы на вопрос "Какого?" (4 пробела)
• "Нет" - ответ на корневой вопрос (без отступа)"""

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔄 Использовать этот пример", callback_data="use_this_example")
    keyboard.button(text="⬅️ Назад", callback_data="back_to_creation")
    
    await callback.message.edit_text(example, parse_mode="HTML", reply_markup=keyboard.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "use_this_example")
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

@dp.callback_query(F.data == "apply_example")
async def apply_example(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    example_text = data.get('example_text', '')
    
    success, poll_data, error_msg = parse_poll_structure(example_text)
    
    if not success:
        await callback.message.edit_text(
            f"❌ Ошибка в примере: {error_msg}\n\nПопробуйте другой формат:",
            reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_creation")).as_markup()
        )
        return
    
    poll_name = data['poll_name']
    poll_data.name = poll_name
    poll_data.created_by = callback.from_user.id
    
    poll_id = storage_manager.add_poll(callback.from_user.id, poll_data)
    
    await state.clear()
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    
    structure_info = f"✅ Опрос <b>'{poll_name}'</b> создан!\n\n"
    structure_info += f"<b>ID опроса:</b> <code>{poll_id}</code>\n"
    structure_info += f"<b>Всего вопросов:</b> {len(poll_data.questions)}"
    
    await callback.message.edit_text(structure_info, parse_mode="HTML", reply_markup=keyboard.as_markup())
    storage_manager.save_to_file()
    await callback.answer()

@dp.callback_query(F.data == "try_with_example")
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

@dp.callback_query(F.data == "back_to_creation")
async def back_to_creation(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    poll_name = data.get('poll_name', '')
    
    instruction = f"Продолжаем создание опроса: <b>{poll_name}</b>\n\n"
    instruction += "Введите структуру опроса с отступами (по 2 пробела на уровень):"
    
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
    
    success, poll_data, error_msg = parse_poll_structure(structure_text)
    
    if not success:
        help_text = f"❌ {error_msg}\n\n"
        
        if "уровен" in error_msg.lower():
            help_text += "<b>Помощь по уровням:</b>\n"
            help_text += "• Корневой вопрос - без отступа и с '?' в конце\n"
            help_text += "• Ответы на корневой вопрос - без отступа\n"
            help_text += "• Вопросы второго уровня - 2 пробела\n"
            help_text += "• Ответы на вопросы второго уровня - 4 пробела\n"
            help_text += "• Вопросы третьего уровня - 6 пробелов\n"
            help_text += "• И так далее...\n\n"
            help_text += "<b>Ключевое правило:</b>\n"
            help_text += "Ответы всегда имеют тот же уровень, что и их вопрос!\n\n"
            help_text += "<b>Правильный пример:</b>\n"
            help_text += "<code>Корневой вопрос?\nОтвет1\nОтвет2\n  Вопрос2?\n    Ответ2.1\n    Ответ2.2\n  Вопрос3?\n    Ответ3.1</code>"
        
        help_text += "\n<b>Проверьте:</b>\n"
        help_text += "1. Корневой вопрос без отступа и с '?' в конце\n"
        help_text += "2. Ответы на корневой вопрос без отступа\n"
        help_text += "3. Вложенные вопросы с соответствующими отступами\n"
        help_text += "4. Ответы на вложенные вопросы с отступами на 2 больше"
        
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
    poll_data.name = poll_name
    poll_data.created_by = message.from_user.id
    
    poll_id = storage_manager.add_poll(message.from_user.id, poll_data)
    
    await state.clear()
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    
    structure_info = f"✅ Опрос <b>'{poll_name}'</b> создан!\n\n"
    structure_info += f"<b>ID опроса:</b> <code>{poll_id}</code>\n"
    structure_info += f"<b>Всего вопросов:</b> {len(poll_data.questions)}"
    
    await message.answer(structure_info, parse_mode="HTML", reply_markup=keyboard.as_markup())
    storage_manager.save_to_file()

@dp.callback_query(F.data == "my_polls")
async def show_my_polls(callback: CallbackQuery):
    admin_id = callback.from_user.id
    user_polls = storage_manager.admin_polls[admin_id]
    
    if not user_polls:
        await callback.message.edit_text(
            "У вас пока нет созданных опросов.",
            reply_markup=InlineKeyboardBuilder()
                .button(text="🏠 Главное меню", callback_data="main_menu")
                .as_markup()
        )
        await callback.answer()
        return
    
    keyboard = InlineKeyboardBuilder()
    for poll_id in user_polls:
        poll = storage_manager.get_poll(poll_id)
        if poll:
            keyboard.button(text=f"📊 {poll.name}", callback_data=f"view_poll_{poll_id}")
    
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    keyboard.adjust(1)
    
    await callback.message.edit_text(
        "Ваши опросы:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("view_poll_"))
async def view_poll_details(callback: CallbackQuery):
    poll_id = int(callback.data.split("_")[2])
    poll = storage_manager.get_poll(poll_id)
    
    if not poll:
        await callback.message.edit_text(
            "Опрос не найден.",
            reply_markup=InlineKeyboardBuilder()
                .button(text="🏠 Главное меню", callback_data="main_menu")
                .as_markup()
        )
        await callback.answer()
        return
    
    details = f"<b>Опрос: {poll.name}</b>\n"
    details += f"<b>ID:</b> {poll_id}\n\n"
    
    for i, question in enumerate(poll.questions):
        details += f"<b>Вопрос {i+1}:</b> {question.text}\n"
        details += f"<b>Уровень:</b> {question.level}\n"
        details += f"<b>Ответы:</b>\n"
        for answer in question.answers:
            next_q = answer.next_question if answer.next_question is not None else 'нет'
            details += f"  - {answer.text} (след. вопрос: {next_q})\n"
        details += "\n"
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🚀 Начать опрос", callback_data=f"start_poll_{poll_id}")
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    
    await callback.message.edit_text(details, parse_mode="HTML", reply_markup=keyboard.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("start_poll_"))
async def start_poll_in_chat(callback: CallbackQuery):
    poll_id = int(callback.data.split("_")[2])
    poll = storage_manager.get_poll(poll_id)
    
    if not poll:
        await callback.message.edit_text(
            "Опрос не найден.",
            reply_markup=InlineKeyboardBuilder()
                .button(text="🏠 Главное меню", callback_data="main_menu")
                .as_markup()
        )
        await callback.answer()
        return
    
    chat_id = callback.message.chat.id
    
    # Проверяем, что это группа
    if callback.message.chat.type not in ['group', 'supergroup']:
        await callback.message.edit_text(
            "❌ Опрос можно начать только в группе!",
            reply_markup=InlineKeyboardBuilder()
                .button(text="🏠 Главное меню", callback_data="main_menu")
                .as_markup()
        )
        await callback.answer()
        return
    
    # Проверяем права администратора
    try:
        member = await bot.get_chat_member(chat_id, callback.from_user.id)
        if not member.status in ['administrator', 'creator']:
            await callback.message.edit_text(
                "❌ Для начала опроса вы должны быть администратором группы!",
                reply_markup=InlineKeyboardBuilder()
                    .button(text="🏠 Главное меню", callback_data="main_menu")
                    .as_markup()
            )
            await callback.answer()
            return
    except Exception:
        await callback.message.edit_text(
            "❌ Не удалось проверить права администратора.",
            reply_markup=InlineKeyboardBuilder()
                .button(text="🏠 Главное меню", callback_data="main_menu")
                .as_markup()
        )
        await callback.answer()
        return
    
    # Начинаем опрос в чате
    storage_manager.active_polls[chat_id] = poll_id
    
    # Сохраняем прогресс пользователей в этом чате
    if chat_id not in storage_manager.user_progress:
        storage_manager.user_progress[chat_id] = {}
    
    # Отправляем первый вопрос
    first_question = poll.questions[0]
    keyboard = InlineKeyboardBuilder()
    for answer in first_question.answers:
        keyboard.button(text=answer.text, callback_data=f"poll_{poll_id}_0_{answer.text}")
    
    await callback.message.edit_text(
        f"<b>Опрос начался!</b>\n\n{first_question.text}",
        parse_mode="HTML",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("poll_"))
async def handle_poll_answer(callback: CallbackQuery):
    # Формат: poll_{poll_id}_{question_idx}_{answer_text}
    parts = callback.data.split("_", 3)
    if len(parts) < 4:
        await callback.answer("Неверный формат данных", show_alert=True)
        return
    
    try:
        poll_id = int(parts[1])
        question_idx = int(parts[2])
        answer_text = parts[3]
    except ValueError:
        await callback.answer("Неверный формат данных", show_alert=True)
        return
    
    poll = storage_manager.get_poll(poll_id)
    if not poll:
        await callback.answer("Опрос не найден", show_alert=True)
        return
    
    # Обновляем результаты
    storage_manager.record_answer(poll_id, question_idx, answer_text)
    
    # Обновляем прогресс пользователя
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    
    if chat_id not in storage_manager.user_progress:
        storage_manager.user_progress[chat_id] = {}
    
    if user_id not in storage_manager.user_progress[chat_id]:
        storage_manager.user_progress[chat_id][user_id] = {'current_poll': poll_id, 'answers': {}}
    
    storage_manager.user_progress[chat_id][user_id]['answers'][question_idx] = answer_text
    
    # Находим следующий вопрос
    current_question = poll.questions[question_idx]
    next_question_idx = None
    
    # Найти индекс ответа, чтобы определить next_question
    for answer in current_question.answers:
        if answer.text == answer_text:
            next_question_idx = answer.next_question
            break
    
    if next_question_idx is not None and next_question_idx < len(poll.questions):
        # Отправляем следующий вопрос
        next_question = poll.questions[next_question_idx]
        keyboard = InlineKeyboardBuilder()
        for answer in next_question.answers:
            keyboard.button(text=answer.text, callback_data=f"poll_{poll_id}_{next_question_idx}_{answer.text}")
        
        await callback.message.edit_text(
            f"{next_question.text}",
            parse_mode="HTML",
            reply_markup=keyboard.as_markup()
        )
    else:
        # Опрос завершен для этого пользователя
        await callback.message.edit_text(
            "✅ Спасибо за участие в опросе!",
            parse_mode="HTML"
        )
    
    storage_manager.save_to_file()
    await callback.answer()

@dp.callback_query(F.data == "show_results")
async def show_results(callback: CallbackQuery):
    admin_id = callback.from_user.id
    user_polls = storage_manager.admin_polls[admin_id]
    
    if not user_polls:
        await callback.message.edit_text(
            "У вас пока нет созданных опросов.",
            reply_markup=InlineKeyboardBuilder()
                .button(text="🏠 Главное меню", callback_data="main_menu")
                .as_markup()
        )
        await callback.answer()
        return
    
    results_text = "<b>Результаты ваших опросов:</b>\n\n"
    
    for poll_id in user_polls:
        poll = storage_manager.get_poll(poll_id)
        if not poll:
            continue
            
        results_text += f"<b>{poll.name} (ID: {poll_id})</b>\n"
        
        for q_idx, question in enumerate(poll.questions):
            results_text += f"\n  <b>Вопрос {q_idx+1}:</b> {question.text}\n"
            for answer_text, count in storage_manager.poll_results[poll_id][q_idx].items():
                results_text += f"    - {answer_text}: {count}\n"
        
        results_text += "\n"
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    
    await callback.message.edit_text(results_text, parse_mode="HTML", reply_markup=keyboard.as_markup())
    await callback.answer()

async def handle_updates():
    """Обработчик обновлений с обработкой исключений"""
    try:
        await dp.start_polling(bot)
    except TelegramRetryAfter as e:
        logger.warning(f"Telegram требует подождать: {e.retry_after} сек.")
        await asyncio.sleep(e.retry_after)
    except TelegramAPIError as e:
        logger.error(f"Ошибка API Telegram: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}")

async def main():
    global bot_instance_running
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("=== Запуск бота для создания опросов ===")
    
    # Загружаем данные
    storage_manager.load_from_file()
    
    try:
        bot_instance_running = True
        logger.info("Запуск polling...")
        await handle_updates()
    except KeyboardInterrupt:
        logger.info("Получен сигнал прерывания")
    except Exception as e:
        logger.error(f"Ошибка при запуске: {e}")
        raise
    finally:
        bot_instance_running = False
        await bot.session.close()
        logger.info("Бот остановлен")

if __name__ == '__main__':
    asyncio.run(main())
