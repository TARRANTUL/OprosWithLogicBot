import os
import logging
import asyncio
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Инициализация бота
API_TOKEN = '8400306221:AAGk7HnyDytn8ymhqTqNWZI8KtxW6CChb-E'
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Глобальные хранилища данных
polls = {}  # {poll_id: {name, questions: [...]}}
poll_id_counter = 1
admin_polls = defaultdict(list)  # {admin_id: [poll_id1, poll_id2, ...]}
poll_results = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))  # {poll_id: {question_idx: {answer: count}}}
user_progress = {}  # {user_id: (poll_id, current_question_idx, [answers])}
active_polls = {}  # {message_id: (poll_id, question_idx)} для удаления сообщений
user_message_map = {}  # {user_id: {poll_id: message_id}} для отслеживания сообщений пользователей

# Состояния для создания опроса
class PollCreationStates(StatesGroup):
    awaiting_poll_name = State()
    awaiting_question_text = State()
    awaiting_answer_options = State()
    awaiting_next_action = State()
    awaiting_new_question = State()
    awaiting_edit_question = State()
    awaiting_edit_answers = State()

# Состояния для редактирования опросов
class PollEditStates(StatesGroup):
    selecting_poll = State()
    selecting_question = State()
    editing_question = State()
    editing_answers = State()

# Загрузка данных из файла
def load_data():
    global polls, admin_polls, poll_results, poll_id_counter
    
    try:
        if os.path.exists('poll_data.json'):
            with open('poll_data.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                polls = data.get('polls', {})
                
                # Восстанавливаем defaultdict для admin_polls
                admin_polls_data = data.get('admin_polls', {})
                for admin_id, poll_ids in admin_polls_data.items():
                    admin_polls[int(admin_id)] = poll_ids
                
                # Восстанавливаем poll_results
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
@dp.callback_query(F.data == "cancel")
async def cmd_cancel(message: Message | CallbackQuery, state: FSMContext):
    if isinstance(message, CallbackQuery):
        await message.message.edit_text("Действие отменено")
        message = message.message
    else:
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

# Команда помощи
@dp.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📖 Справка по использованию бота:\n\n"
        "1. Создание опроса:\n"
        "   - Используйте /start или кнопку 'Создать опрос'\n"
        "   - Следуйте инструкциям для добавления вопросов и ответов\n"
        "   - Для каждого ответа настройте переход к следующему вопросу или завершение опроса\n\n"
        "2. Запуск опроса:\n"
        "   - Выберите опрос в меню 'Мои опросы'\n"
        "   - Нажмите 'Запустить опрос'\n"
        "   - Отправьте полученное сообщение в нужный чат\n\n"
        "3. Просмотр результатов:\n"
        "   - Используйте меню 'Результаты' для просмотра статистики\n\n"
        "4. Управление опросами:\n"
        "   - Редактирование и удаление доступно через меню 'Мои опросы'\n\n"
        "5. Отмена действий:\n"
        "   - Используйте /cancel для отмены текущего действия"
    )
    await message.answer(help_text)

# Главное меню
@dp.callback_query(F.data == "main_menu")
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
@dp.callback_query(F.data == "create_poll")
async def create_poll_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PollCreationStates.awaiting_poll_name)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data="main_menu")
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
    
    # Валидация названия
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
    keyboard.button(text="🔙 Назад", callback_data="back_to_poll_name")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    
    await message.answer(
        "Введите текст первого вопроса:",
        reply_markup=keyboard.as_markup()
    )

# Назад к вводу названия опроса
@dp.callback_query(PollCreationStates.awaiting_question_text, F.data == "back_to_poll_name")
async def back_to_poll_name(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PollCreationStates.awaiting_poll_name)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data="main_menu")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    
    await callback.message.edit_text(
        "Введите название опроса:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Обработка текста вопроса
@dp.message(PollCreationStates.awaiting_question_text)
async def process_question_text(message: Message, state: FSMContext):
    question_text = message.text.strip()
    
    # Валидация вопроса
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
    keyboard.button(text="🔙 Назад", callback_data="back_to_question_text")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    
    await message.answer(
        "Введите варианты ответов через запятую (например: Да, Нет, Не знаю):",
        reply_markup=keyboard.as_markup()
    )

# Назад к вводу текста вопроса
@dp.callback_query(PollCreationStates.awaiting_answer_options, F.data == "back_to_question_text")
async def back_to_question_text(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    poll_data = data['poll_data']
    
    # Удаляем последний вопрос
    if poll_data['questions']:
        poll_data['questions'].pop()
    
    await state.update_data(poll_data=poll_data)
    await state.set_state(PollCreationStates.awaiting_question_text)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data="back_to_poll_name")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    
    await callback.message.edit_text(
        "Введите текст вопроса:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Обработка вариантов ответов
@dp.message(PollCreationStates.awaiting_answer_options)
async def process_answer_options(message: Message, state: FSMContext):
    answers = [ans.strip() for ans in message.text.split(',') if ans.strip()]
    
    # Валидация ответов
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
    await state.set_state(PollCreationStates.awaiting_next_action)
    await show_next_action_menu(message, state)

# Показать меню следующих действий
async def show_next_action_menu(message: Message, state: FSMContext):
    data = await state.get_data()
    poll_data = data['poll_data']
    current_question = poll_data['questions'][-1]
    
    keyboard = InlineKeyboardBuilder()
    for i, ans in enumerate(current_question['answers']):
        status = "✅" if ans['next_question'] is not None else "❌"
        keyboard.button(
            text=f"{i+1}. {ans['text']} {status}",
            callback_data=f"setup_answer_{i}"
        )
    
    keyboard.adjust(1)
    keyboard.row(
        InlineKeyboardButton(text="➕ Добавить еще вопрос", callback_data="add_another_question"),
        InlineKeyboardButton(text="✅ Завершить опрос", callback_data="finish_poll")
    )
    keyboard.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_answer_options"),
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

# Назад к вводу вариантов ответов
@dp.callback_query(PollCreationStates.awaiting_next_action, F.data == "back_to_answer_options")
async def back_to_answer_options(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    poll_data = data['poll_data']
    current_question = poll_data['questions'][-1]
    
    # Удаляем ответы текущего вопроса
    current_question['answers'] = []
    
    await state.update_data(poll_data=poll_data)
    await state.set_state(PollCreationStates.awaiting_answer_options)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data="back_to_question_text")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    
    await callback.message.edit_text(
        "Введите варианты ответов через запятую:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Настройка действия для ответа
@dp.callback_query(PollCreationStates.awaiting_next_action, F.data.startswith("setup_answer_"))
async def setup_answer_action(callback: CallbackQuery, state: FSMContext):
    ans_idx = int(callback.data.split('_')[-1])
    data = await state.get_data()
    poll_data = data['poll_data']
    current_question = poll_data['questions'][-1]
    selected_answer = current_question['answers'][ans_idx]
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text="➕ Добавить следующий вопрос",
        callback_data=f"add_question_{ans_idx}"
    )
    keyboard.button(
        text="⏹️ Завершить опрос для этого ответа",
        callback_data=f"end_poll_{ans_idx}"
    )
    keyboard.button(
        text="🔙 Назад",
        callback_data="back_to_action_menu"
    )
    keyboard.button(
        text="❌ Отмена",
        callback_data="cancel"
    )
    
    await callback.message.edit_text(
        f"Настройка ответа: *{selected_answer['text']}*\n\n"
        "Выберите действие при выборе этого ответа:",
        reply_markup=keyboard.as_markup(),
        parse_mode="Markdown"
    )
    await callback.answer()

# Назад к меню действий
@dp.callback_query(F.data == "back_to_action_menu")
async def back_to_action_menu(callback: CallbackQuery, state: FSMContext):
    await show_next_action_menu(callback.message, state)
    await callback.answer()

# Добавление вопроса для ответа
@dp.callback_query(PollCreationStates.awaiting_next_action, F.data.startswith("add_question_"))
async def add_question_for_answer(callback: CallbackQuery, state: FSMContext):
    ans_idx = int(callback.data.split('_')[-1])
    await state.update_data(current_answer_index=ans_idx)
    await state.set_state(PollCreationStates.awaiting_new_question)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data="back_to_action_menu")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    
    await callback.message.edit_text(
        "Введите текст следующего вопроса:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Обработка нового вопроса
@dp.message(PollCreationStates.awaiting_new_question)
async def process_new_question(message: Message, state: FSMContext):
    new_question_text = message.text.strip()
    
    # Валидация вопроса
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
    
    # Связываем ответ с новым вопросом
    current_question = poll_data['questions'][-2]
    current_question['answers'][ans_idx]['next_question'] = len(poll_data['questions']) - 1
    
    await state.update_data(poll_data=poll_data)
    await state.set_state(PollCreationStates.awaiting_answer_options)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data="back_to_new_question")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    
    await message.answer(
        "Введите варианты ответов для нового вопроса через запятую:",
        reply_markup=keyboard.as_markup()
    )

# Назад от нового вопроса
@dp.callback_query(PollCreationStates.awaiting_answer_options, F.data == "back_to_new_question")
async def back_to_new_question(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    poll_data = data['poll_data']
    
    # Удаляем последний вопрос
    if len(poll_data['questions']) > 1:
        poll_data['questions'].pop()
    
    await state.update_data(poll_data=poll_data)
    await state.set_state(PollCreationStates.awaiting_next_action)
    await show_next_action_menu(callback.message, state)
    await callback.answer()

# Завершение опроса для ответа
@dp.callback_query(PollCreationStates.awaiting_next_action, F.data.startswith("end_poll_"))
async def end_poll_for_answer(callback: CallbackQuery, state: FSMContext):
    ans_idx = int(callback.data.split('_')[-1])
    data = await state.get_data()
    poll_data = data['poll_data']
    current_question = poll_data['questions'][-1]
    
    # Устанавливаем завершение опроса для ответа
    current_question['answers'][ans_idx]['next_question'] = None
    
    await state.update_data(poll_data=poll_data)
    await show_next_action_menu(callback.message, state)
    await callback.answer()

# Добавление еще одного вопроса
@dp.callback_query(PollCreationStates.awaiting_next_action, F.data == "add_another_question")
async def add_another_question(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PollCreationStates.awaiting_question_text)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data="back_to_action_menu")
    keyboard.button(text="❌ Отмена", callback_data="cancel")
    
    await callback.message.edit_text(
        "Введите текст следующего вопроса:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Завершение создания опроса
@dp.callback_query(PollCreationStates.awaiting_next_action, F.data == "finish_poll")
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
    
    # Сохраняем данные
    save_data()

# Показать мои опросы
@dp.callback_query(F.data == "my_polls")
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
        # Обрезаем длинное название
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
@dp.callback_query(F.data.startswith("view_poll_"))
async def view_poll_details(callback: CallbackQuery):
    poll_id = int(callback.data.split('_')[-1])
    if poll_id not in polls:
        await callback.answer("Ошибка: опрос не найден")
        return
    
    poll = polls[poll_id]
    text = f"📋 Опрос: *{poll['name']}*\nID: `{poll_id}`\n\n"
    
    # Формируем структуру опроса
    def build_structure(question_idx, level=0):
        nonlocal text
        question = poll['questions'][question_idx]
        indent = "  " * level
        
        text += f"{indent}❓ {question_idx + 1}. {question['text']}\n"
        
        for i, ans in enumerate(question['answers']):
            text += f"{indent}    ➡️ {ans['text']}"
            
            if ans['next_question'] is not None:
                text += " → следующий вопрос\n"
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
    
    # Разделяем текст если он слишком длинный
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

# Удаление опроса
@dp.callback_query(F.data.startswith("delete_poll_"))
async def delete_poll(callback: CallbackQuery):
    poll_id = int(callback.data.split('_')[-1])
    admin_id = callback.from_user.id
    
    if poll_id not in polls or poll_id not in admin_polls[admin_id]:
        await callback.answer("Ошибка: опрос не найден")
        return
    
    # Создаем клавиатуру подтверждения
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✅ Да, удалить", callback_data=f"confirm_delete_{poll_id}")
    keyboard.button(text="❌ Нет, отменить", callback_data=f"view_poll_{poll_id}")
    
    await callback.message.edit_text(
        f"❓ Вы уверены, что хотите удалить опрос *'{polls[poll_id]['name']}'*?",
        parse_mode="Markdown",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Подтверждение удаления опроса
@dp.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_poll(callback: CallbackQuery):
    poll_id = int(callback.data.split('_')[-1])
    admin_id = callback.from_user.id
    
    if poll_id not in polls or poll_id not in admin_polls[admin_id]:
        await callback.answer("Ошибка: опрос не найден")
        return
    
    poll_name = polls[poll_id]['name']
    
    # Удаляем опрос
    del polls[poll_id]
    admin_polls[admin_id].remove(poll_id)
    
    # Удаляем результаты
    if poll_id in poll_results:
        del poll_results[poll_id]
    
    # Сохраняем изменения
    save_data()
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    
    await callback.message.edit_text(
        f"✅ Опрос '{poll_name}' успешно удален!",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Запуск опроса
@dp.callback_query(F.data.startswith("start_poll_"))
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
    keyboard.row(InlineKeyboardButton(
        text="❌ Отмена (только для админа)",
        callback_data=f"admin_cancel_{poll_id}"
    ))
    
    await callback.message.edit_text(
        f"🚀 Опрос начат: *{poll['name']}*\n\n"
        f"1. {first_question['text']}",
        reply_markup=keyboard.as_markup(),
        parse_mode="Markdown"
    )
    await callback.answer()

# Обработка ответов на опрос
@dp.callback_query(F.data.startswith("poll_"))
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
            user_progress[user_id] = (poll_id, question_idx, [answer['text']])
        else:
            _, _, answers = user_progress[user_id]
            answers.append(answer['text'])
            user_progress[user_id] = (poll_id, question_idx, answers)
        
        # Удаляем сообщение с опросом для этого пользователя
        try:
            await callback.message.delete()
        except Exception as e:
            logger.error(f"Ошибка удаления сообщения: {e}")
        
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
            
            # Отправляем следующий вопрос
            msg = await callback.message.answer(
                f"{next_idx + 1}. {next_question['text']}",
                reply_markup=keyboard.as_markup()
            )
            
            # Сохраняем информацию о сообщении для возможного удаления
            if user_id not in user_message_map:
                user_message_map[user_id] = {}
            user_message_map[user_id][poll_id] = msg.message_id
            
        else:
            # Завершение опроса
            await callback.message.answer("Спасибо за прохождение опроса! 🙌")
            # Сохраняем финальный результат
            user_progress[user_id] = (poll_id, None, user_progress[user_id][2])
        
        # Сохраняем данные
        save_data()
        
    except Exception as e:
        logger.error(f"Ошибка обработки ответа: {e}")
        await callback.answer("Произошла ошибка. Попробуйте еще раз.")
    
    await callback.answer()

# Отмена опроса администратором
@dp.callback_query(F.data.startswith("admin_cancel_"))
async def admin_cancel_poll(callback: CallbackQuery):
    poll_id = int(callback.data.split('_')[-1])
    admin_id = callback.from_user.id
    
    # Проверяем, является ли пользователь администратором опроса
    if poll_id not in admin_polls[admin_id]:
        await callback.answer("❌ Только администратор опроса может отменить его")
        return
    
    # Удаляем сообщение с опросом
    try:
        await callback.message.delete()
    except Exception as e:
        logger.error(f"Ошибка удаления сообщения: {e}")
    
    await callback.message.answer("Опрос отменен администратором")
    await callback.answer()

# Показать результаты
@dp.callback_query(F.data == "show_results")
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
        # Обрезаем длинное название
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

# Показать результаты конкретного опроса
@dp.callback_query(F.data.startswith("results_"))
async def show_poll_results(callback: CallbackQuery):
    poll_id = int(callback.data.split('_')[-1])
    if poll_id not in polls:
        await callback.answer("Ошибка: опрос не найден")
        return
    
    poll = polls[poll_id]
    results = poll_results[poll_id]
    
    report = f"📊 Результаты опроса *{poll['name']}*:\n\n"
    total_participants = 0
    
    # Считаем общее количество участников
    if results:
        first_question_results = results.get(0, {})
        total_participants = sum(first_question_results.values())
        report += f"👥 Всего участников: {total_participants}\n\n"
    
    # Функция для рекурсивного построения отчета
    def build_report(question_idx, level=0):
        nonlocal report
        question = poll['questions'][question_idx]
        indent = "  " * level
        
        report += f"{indent}❓ {question_idx + 1}. {question['text']}?\n"
        
        if question_idx in results:
            question_results = results[question_idx]
            total_votes = sum(question_results.values())
            
            for ans_text, count in question_results.items():
                # Определяем действие для ответа
                next_action = "опрос завершен"
                for ans in question['answers']:
                    if ans['text'] == ans_text:
                        if ans['next_question'] is not None:
                            next_q = ans['next_question'] + 1
                            next_action = f"переход к вопросу {next_q}"
                        break
                
                percentage = (count / total_votes * 100) if total_votes > 0 else 0
                report += f"{indent}   • {ans_text} — {count} голосов ({percentage:.1f}%) ({next_action})\n"
                
                # Рекурсивно добавляем результаты для следующих вопросов
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
    
    # Если отчет слишком длинный, разбиваем на части
    if len(report) > 4000:
        parts = [report[i:i+4000] for i in range(0, len(report), 4000)]
        for part in parts[:-1]:
            await callback.message.answer(part, parse_mode="Markdown")
        report = parts[-1]
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📋 Мои опросы", callback_data="my_polls")
    keyboard.button(text="🏠 Главное меню", callback_data="main_menu")
    
    await callback.message.edit_text(
        report,
        parse_mode="Markdown",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Редактирование опроса
@dp.callback_query(F.data.startswith("edit_poll_"))
async def edit_poll_start(callback: CallbackQuery, state: FSMContext):
    poll_id = int(callback.data.split('_')[-1])
    admin_id = callback.from_user.id
    
    if poll_id not in polls or poll_id not in admin_polls[admin_id]:
        await callback.answer("Ошибка: опрос не найден")
        return
    
    await state.set_state(PollEditStates.selecting_question)
    await state.update_data(edit_poll_id=poll_id)
    
    poll = polls[poll_id]
    keyboard = InlineKeyboardBuilder()
    
    for i, question in enumerate(poll['questions']):
        keyboard.button(
            text=f"{i+1}. {question['text'][:30]}{'...' if len(question['text']) > 30 else ''}",
            callback_data=f"edit_question_{i}"
        )
    
    keyboard.adjust(1)
    keyboard.button(text="🔙 Назад", callback_data=f"view_poll_{poll_id}")
    
    await callback.message.edit_text(
        f"Выберите вопрос для редактирования в опросе '{poll['name']}':",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Редактирование вопроса
@dp.callback_query(PollEditStates.selecting_question, F.data.startswith("edit_question_"))
async def edit_question(callback: CallbackQuery, state: FSMContext):
    question_idx = int(callback.data.split('_')[-1])
    data = await state.get_data()
    poll_id = data['edit_poll_id']
    
    await state.set_state(PollEditStates.editing_question)
    await state.update_data(edit_question_idx=question_idx)
    
    poll = polls[poll_id]
    question = poll['questions'][question_idx]
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✏️ Редактировать текст", callback_data="edit_question_text")
    keyboard.button(text="✏️ Редактировать ответы", callback_data="edit_question_answers")
    keyboard.button(text="🔙 Назад", callback_data=f"edit_poll_{poll_id}")
    
    await callback.message.edit_text(
        f"Вопрос {question_idx + 1}: {question['text']}\n\n"
        "Что вы хотите отредактировать?",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Редактирование текста вопроса
@dp.callback_query(PollEditStates.editing_question, F.data == "edit_question_text")
async def edit_question_text(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    poll_id = data['edit_poll_id']
    question_idx = data['edit_question_idx']
    
    poll = polls[poll_id]
    question = poll['questions'][question_idx]
    
    await state.set_state(PollEditStates.editing_question)
    await state.update_data(editing_field="question_text")
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data=f"edit_question_{question_idx}")
    
    await callback.message.edit_text(
        f"Текущий текст вопроса: {question['text']}\n\n"
        "Введите новый текст вопроса:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Редактирование ответов вопроса
@dp.callback_query(PollEditStates.editing_question, F.data == "edit_question_answers")
async def edit_question_answers(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    poll_id = data['edit_poll_id']
    question_idx = data['edit_question_idx']
    
    poll = polls[poll_id]
    question = poll['questions'][question_idx]
    
    await state.set_state(PollEditStates.editing_answers)
    
    # Создаем клавиатуру с ответами
    keyboard = InlineKeyboardBuilder()
    for i, answer in enumerate(question['answers']):
        keyboard.button(
            text=f"{i+1}. {answer['text']}",
            callback_data=f"edit_answer_{i}"
        )
    
    keyboard.adjust(1)
    keyboard.button(text="➕ Добавить ответ", callback_data="add_answer")
    keyboard.button(text="🔙 Назад", callback_data=f"edit_question_{question_idx}")
    
    await callback.message.edit_text(
        f"Выберите ответ для редактирования в вопросе:\n\n{question['text']}",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Обработка ввода нового текста вопроса
@dp.message(PollEditStates.editing_question)
async def process_edited_question_text(message: Message, state: FSMContext):
    new_text = message.text.strip()
    
    # Валидация вопроса
    is_valid, error_msg = validate_question_text(new_text)
    if not is_valid:
        await message.answer(f"❌ {error_msg}. Попробуйте еще раз:")
        return
    
    data = await state.get_data()
    poll_id = data['edit_poll_id']
    question_idx = data['edit_question_idx']
    
    # Обновляем текст вопроса
    polls[poll_id]['questions'][question_idx]['text'] = new_text
    save_data()
    
    await state.set_state(PollEditStates.selecting_question)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data=f"edit_poll_{poll_id}")
    
    await message.answer(
        "✅ Текст вопроса успешно обновлен!",
        reply_markup=keyboard.as_markup()
    )

# Редактирование конкретного ответа
@dp.callback_query(PollEditStates.editing_answers, F.data.startswith("edit_answer_"))
async def edit_specific_answer(callback: CallbackQuery, state: FSMContext):
    answer_idx = int(callback.data.split('_')[-1])
    data = await state.get_data()
    poll_id = data['edit_poll_id']
    question_idx = data['edit_question_idx']
    
    poll = polls[poll_id]
    question = poll['questions'][question_idx]
    answer = question['answers'][answer_idx]
    
    await state.update_data(edit_answer_idx=answer_idx)
    await state.set_state(PollEditStates.editing_answers)
    await state.update_data(editing_field="answer_text")
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data="edit_question_answers")
    
    await callback.message.edit_text(
        f"Текущий текст ответа: {answer['text']}\n\n"
        "Введите новый текст ответа:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Обработка ввода нового текста ответа
@dp.message(PollEditStates.editing_answers)
async def process_edited_answer_text(message: Message, state: FSMContext):
    new_text = message.text.strip()
    
    # Валидация ответа
    if not new_text or len(new_text) > 50:
        await message.answer("❌ Текст ответа не может быть пустым или превышать 50 символов. Попробуйте еще раз:")
        return
    
    data = await state.get_data()
    poll_id = data['edit_poll_id']
    question_idx = data['edit_question_idx']
    answer_idx = data['edit_answer_idx']
    
    # Обновляем текст ответа
    polls[poll_id]['questions'][question_idx]['answers'][answer_idx]['text'] = new_text
    save_data()
    
    await state.set_state(PollEditStates.editing_answers)
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data="edit_question_answers")
    
    await message.answer(
        "✅ Текст ответа успешно обновлен!",
        reply_markup=keyboard.as_markup()
    )

# Добавление нового ответа
@dp.callback_query(PollEditStates.editing_answers, F.data == "add_answer")
async def add_new_answer(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    poll_id = data['edit_poll_id']
    question_idx = data['edit_question_idx']
    
    poll = polls[poll_id]
    question = poll['questions'][question_idx]
    
    # Добавляем новый ответ
    question['answers'].append({
        'text': "Новый ответ",
        'next_question': None
    })
    save_data()
    
    await state.update_data(edit_answer_idx=len(question['answers']) - 1)
    await state.set_state(PollEditStates.editing_answers)
    await state.update_data(editing_field="answer_text")
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data="edit_question_answers")
    
    await callback.message.edit_text(
        "Введите текст для нового ответа:",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()

# Запуск бота
async def main():
    # Загружаем данные при запуске
    load_data()
    
    # Запускаем бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
