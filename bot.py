import os
import logging
import asyncio
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Токен бота
BOT_TOKEN = "8400306221:AAGk7HnyDytn8ymhqTqNWZI8KtxW6CChb-E"

# Инициализация бота и диспетчера для aiogram 3.x
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Определение состояний для опроса
class PollStates(StatesGroup):
    AGE = State()
    GENDER = State()
    EDUCATION = State()
    INTERESTS = State()

# Обработчик команды /start
@dp.message(types.F.text == '/start')
async def cmd_start(message: types.Message):
    logger.info(f"Пользователь {message.from_user.id} запустил бота")
    
    welcome_text = """
👋 Привет! Добро пожаловать в бот для опросов!

Я помогу вам пройти небольшой опрос. Для начала используйте команду /poll

Доступные команды:
/poll - Начать опрос
/help - Получить справку
    """
    
    await message.answer(welcome_text)

# Обработчик команды /help
@dp.message(types.F.text == '/help')
async def cmd_help(message: types.Message):
    help_text = """
ℹ️ Справка по боту:

/poll - Начать опрос. Вам будут заданы вопросы о возрасте, поле, образовании и интересах.

Опрос состоит из 4 простых вопросов. Вы можете прервать опрос в любой момент, отправив /cancel
    """
    await message.answer(help_text)

# Обработчик команды /poll - начало опроса
@dp.message(types.F.text == '/poll')
async def start_poll(message: types.Message, state: FSMContext):
    logger.info(f"Пользователь {message.from_user.id} начал опрос")
    
    await message.answer("📝 Начинаем опрос!\n\nПожалуйста, укажите ваш возраст:")
    await state.set_state(PollStates.AGE)

# Обработчик отмены опроса
@dp.message(types.F.text == '/cancel')
@dp.message(types.F.text.casefold() == 'отмена')
async def cancel_poll(message: types.Message, state: FSMContext):
    logger.info(f"Пользователь {message.from_user.id} отменил опрос")
    
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Опрос не активен.")
        return
    
    await state.clear()
    await message.answer("❌ Опрос прерван. Вы можете начать заново с помощью /poll")

# Обработчик возраста
@dp.message(PollStates.AGE)
async def process_age(message: types.Message, state: FSMContext):
    age_text = message.text.strip()
    
    # Проверка валидности возраста
    if not age_text.isdigit() or not (1 <= int(age_text) <= 120):
        await message.answer("❌ Пожалуйста, введите корректный возраст (число от 1 до 120):")
        return
    
    age = int(age_text)
    await state.update_data(age=age)
    logger.info(f"Пользователь {message.from_user.id} указал возраст: {age}")
    
    # Создаем клавиатуру для выбора пола
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Мужской"), types.KeyboardButton(text="Женский")],
            [types.KeyboardButton(text="Другой")]
        ],
        resize_keyboard=True
    )
    
    await message.answer("Выберите ваш пол:", reply_markup=keyboard)
    await state.set_state(PollStates.GENDER)

# Обработчик пола
@dp.message(PollStates.GENDER)
async def process_gender(message: types.Message, state: FSMContext):
    gender = message.text.strip()
    valid_genders = ["Мужской", "Женский", "Другой"]
    
    if gender not in valid_genders:
        await message.answer("❌ Пожалуйста, выберите пол из предложенных вариантов:")
        return
    
    await state.update_data(gender=gender)
    logger.info(f"Пользователь {message.from_user.id} указал пол: {gender}")
    
    # Создаем клавиатуру для выбора образования
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Среднее"), types.KeyboardButton(text="Среднее специальное")],
            [types.KeyboardButton(text="Высшее"), types.KeyboardButton(text="Учусь")],
            [types.KeyboardButton(text="Другое")]
        ],
        resize_keyboard=True
    )
    
    await message.answer("Укажите ваше образование:", reply_markup=keyboard)
    await state.set_state(PollStates.EDUCATION)

# Обработчик образования
@dp.message(PollStates.EDUCATION)
async def process_education(message: types.Message, state: FSMContext):
    education = message.text.strip()
    valid_education = ["Среднее", "Среднее специальное", "Высшее", "Учусь", "Другое"]
    
    if education not in valid_education:
        await message.answer("❌ Пожалуйста, выберите вариант из предложенных:")
        return
    
    await state.update_data(education=education)
    logger.info(f"Пользователь {message.from_user.id} указал образование: {education}")
    
    # Убираем клавиатуру
    keyboard = types.ReplyKeyboardRemove()
    
    await message.answer("📚 Расскажите о ваших интересах или увлечениях:", reply_markup=keyboard)
    await state.set_state(PollStates.INTERESTS)

# Обработчик интересов (завершение опроса)
@dp.message(PollStates.INTERESTS)
async def process_interests(message: types.Message, state: FSMContext):
    interests = message.text.strip()
    
    if len(interests) < 5:
        await message.answer("❌ Пожалуйста, напишите немного подробнее о ваших интересах:")
        return
    
    await state.update_data(interests=interests)
    logger.info(f"Пользователь {message.from_user.id} указал интересы: {interests}")
    
    # Получаем все данные из состояния
    user_data = await state.get_data()
    
    # Формируем результат опроса
    result_text = f"""
✅ Опрос завершен! Спасибо за участие!

📊 Ваши ответы:
• Возраст: {user_data.get('age')}
• Пол: {user_data.get('gender')}
• Образование: {user_data.get('education')}
• Интересы: {user_data.get('interests')}

Вы можете пройти опрос еще раз с помощью /poll
    """
    
    await message.answer(result_text)
    await state.clear()
    
    logger.info(f"Опрос пользователя {message.from_user.id} завершен успешно")

# Обработчик любых других сообщений
@dp.message()
async def handle_other_messages(message: types.Message):
    logger.info(f"Получено сообщение от {message.from_user.id}: {message.text}")
    
    response_text = """
🤖 Я бот для проведения опросов.

Используйте команды:
/start - Начало работы
/poll - Начать опрос
/help - Получить справку
/cancel - Отменить текущий опрос
    """
    
    await message.answer(response_text)

# HTTP-сервер для Render.com
async def handle_health_check(request):
    """Обработчик health-check запросов от Render"""
    return web.Response(text="Bot is running!")

async def main():
    """Основная функция запуска"""
    logger.info("=== Запуск бота на Render.com ===")
    
    try:
        # Создаем HTTP-сервер
        app = web.Application()
        app.router.add_get('/health', handle_health_check)
        app.router.add_get('/', handle_health_check)
        
        # Получаем порт из переменных окружения
        port = int(os.environ.get('PORT', 5000))
        
        # Запускаем HTTP-сервер в фоне
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logger.info(f"HTTP-сервер запущен на порту {port}")
        
        # Запускаем бота
        logger.info("Запуск polling бота...")
        await dp.start_polling(bot)
        
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")
        raise
    finally:
        await bot.session.close()
        logger.info("Бот остановлен")

if __name__ == '__main__':
    asyncio.run(main())
