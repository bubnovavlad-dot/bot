import os
import asyncio
import logging
import tempfile
import base64

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

from dotenv import load_dotenv
from openai import AsyncOpenAI
import vk_api

# ==================== ЗАГРУЗКА ПЕРЕМЕННЫХ ====================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
AMVERA_API_KEY = os.getenv("AMVERA_API_KEY")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
VK_ACCESS_TOKEN = os.getenv("VK_ACCESS_TOKEN")
VK_GROUP_ID = os.getenv("VK_GROUP_ID")

if not BOT_TOKEN or not AMVERA_API_KEY:
    raise ValueError("Не найдены BOT_TOKEN или AMVERA_API_KEY в .env")

# ==================== НАСТРОЙКА КЛИЕНТА AMVERA ====================
amvera_client = AsyncOpenAI(
    api_key=AMVERA_API_KEY,
    base_url="https://inference.waw0.amvera.ru/v1",
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ==================== FSM ====================
class PostCreation(StatesGroup):
    waiting_for_edit_request = State()

user_posts = {}

# ==================== АНАЛИЗ ФОТО ====================
async def analyze_image(image_path: str) -> str:
    try:
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")
        response = await amvera_client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Опиши это изображение коротко, но содержательно: что на нём изображено, главный объект, действие, настроение, контекст. Не более 3 предложений."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}}
                    ]
                }
            ],
            max_tokens=300,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Ошибка анализа изображения: {e}")
        return None

# ==================== ГЕНЕРАЦИЯ ПОСТА ====================
async def generate_post_text(description: str) -> str:
    prompt = f"""
    Ты — креативный SMM-менеджер.
    Вот описание контента: {description}
    Напиши пост для соцсетей (Telegram, VK, Дзен).

    Пост должен содержать:
    1. Цепляющий заголовок.
    2. Основной текст.
    3. 3-5 хештегов.
    4. Призыв к действию.

    Отвечай только текстом поста.
    """
    try:
        response = await amvera_client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": "Ты — опытный SMM-специалист."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=1000,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка генерации: {e}")
        return "❌ Не удалось сгенерировать текст."

# ==================== РЕДАКТИРОВАНИЕ ЧЕРЕЗ GPT ====================
async def edit_post_text(current_text: str, edit_request: str) -> str:
    prompt = f"""
    Ты — редактор текстов для соцсетей.
    Вот текущий текст поста:
    ---
    {current_text}
    ---
    Запрос пользователя на редактирование: {edit_request}

    Перепиши пост согласно запросу. Сохрани общую структуру.
    Отвечай только итоговым текстом поста.
    """
    try:
        response = await amvera_client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": "Ты — опытный редактор SMM-текстов."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=1000,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка редактирования: {e}")
        return None

# ==================== ПУБЛИКАЦИЯ ====================
async def publish_to_telegram(text: str, media_path: str = None, media_type: str = None):
    if not TG_CHAT_ID:
        return False
    try:
        if media_path and os.path.exists(media_path):
            media_file = FSInputFile(media_path)
            # Если текст длинный — отправляем фото без подписи, а текст отдельно
            if len(text) > 1000:
                if media_type == 'photo':
                    await bot.send_photo(chat_id=TG_CHAT_ID, photo=media_file)
                elif media_type == 'video':
                    await bot.send_video(chat_id=TG_CHAT_ID, video=media_file)
                await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")
            else:
                if media_type == 'photo':
                    await bot.send_photo(chat_id=TG_CHAT_ID, photo=media_file, caption=text, parse_mode="Markdown")
                elif media_type == 'video':
                    await bot.send_video(chat_id=TG_CHAT_ID, video=media_file, caption=text, parse_mode="Markdown")
                else:
                    await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")
        else:
            await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="Markdown")
        return True
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

async def publish_to_vk(text: str, media_path: str = None, media_type: str = None):
    if not VK_ACCESS_TOKEN or not VK_GROUP_ID:
        return False
    try:
        vk_session = vk_api.VkApi(token=VK_ACCESS_TOKEN)
        vk = vk_session.get_api()
        attachments = []
        if media_path and media_type == 'photo' and os.path.exists(media_path):
            upload_url = vk.photos.getWallUploadServer(group_id=VK_GROUP_ID)['upload_url']
            with open(media_path, 'rb') as f:
                upload_data = vk.http.post(upload_url, files={'photo': f})
            photo_data = vk.photos.saveWallPhoto(group_id=VK_GROUP_ID, **upload_data)
            attachments = [f"photo{photo['owner_id']}_{photo['id']}" for photo in photo_data]
        vk.wall.post(owner_id=VK_GROUP_ID, message=text, attachments=','.join(attachments))
        return True
    except Exception as e:
        logger.error(f"VK error: {e}")
        return False

async def publish_to_all(text: str, media_path: str = None, media_type: str = None):
    results = []
    if TG_CHAT_ID:
        results.append(await publish_to_telegram(text, media_path, media_type))
    if VK_ACCESS_TOKEN and VK_GROUP_ID:
        results.append(await publish_to_vk(text, media_path, media_type))
    return all(results)

# ==================== ОТПРАВКА ЧЕРНОВИКА С УЧЁТОМ ДЛИНЫ ====================
async def send_draft(target, user_id: int, new_text: str = None):
    user_data = user_posts.get(user_id)
    if not user_data:
        return
    text = new_text if new_text else user_data.get('text', '')
    if not text:
        return

    media_path = user_data.get('media_path')
    media_type = user_data.get('media_type')

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Опубликовать везде", callback_data=f"approve_{user_id}")
    builder.button(text="✏️ Редактировать через ИИ", callback_data=f"edit_req_{user_id}")
    builder.adjust(1)

    # Функция для отправки с учётом длины caption
    async def send_with_media(msg, media_file, caption_text):
        if len(caption_text) > 1000:
            # Отправляем медиа без подписи, а текст отдельным сообщением
            if media_type == 'photo':
                await msg.answer_photo(photo=media_file)
            elif media_type == 'video':
                await msg.answer_video(video=media_file)
            await msg.answer(f"📝 *Черновик поста:*\n\n{caption_text}", parse_mode="Markdown", reply_markup=builder.as_markup())
        else:
            if media_type == 'photo':
                await msg.answer_photo(photo=media_file, caption=f"📝 *Черновик поста:*\n\n{caption_text}", parse_mode="Markdown", reply_markup=builder.as_markup())
            elif media_type == 'video':
                await msg.answer_video(video=media_file, caption=f"📝 *Черновик поста:*\n\n{caption_text}", parse_mode="Markdown", reply_markup=builder.as_markup())
            else:
                await msg.answer(f"📝 *Черновик поста:*\n\n{caption_text}", parse_mode="Markdown", reply_markup=builder.as_markup())

    if isinstance(target, Message):
        msg = target
        if media_path and os.path.exists(media_path):
            media_file = FSInputFile(media_path)
            await send_with_media(msg, media_file, text)
        else:
            await msg.answer(f"📝 *Черновик поста:*\n\n{text}", parse_mode="Markdown", reply_markup=builder.as_markup())
    else:  # CallbackQuery
        call = target
        if media_path and os.path.exists(media_path):
            media_file = FSInputFile(media_path)
            await send_with_media(call.message, media_file, text)
        else:
            await call.message.answer(f"📝 *Черновик поста:*\n\n{text}", parse_mode="Markdown", reply_markup=builder.as_markup())
        await call.answer()

# ==================== ОБРАБОТЧИКИ ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Отправь мне **фото** (можно с текстовой подписью).\n"
        "Я проанализирую изображение и напишу пост для Telegram, VK и Дзен.\n\n"
        "Если захочешь изменить текст — нажми «Редактировать через ИИ» и напиши, что нужно исправить.\n"
        "Например: «сделай короче», «добавь юмор», «напиши от первого лица»."
    )

@dp.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    processing_msg = await message.answer("🖼 Анализирую изображение...")
    tmp_path = None
    try:
        caption = message.caption or ""
        file_id = message.photo[-1].file_id
        file = await bot.get_file(file_id)
        fd, tmp_path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        await bot.download_file(file.file_path, destination=tmp_path)

        image_description = await analyze_image(tmp_path)
        if not image_description:
            await processing_msg.edit_text("❌ Не удалось распознать изображение. Попробуйте ещё.")
            return

        full_description = image_description
        if caption:
            full_description += f"\n\nДополнительный запрос пользователя: {caption}"

        await processing_msg.edit_text("✍️ Генерирую текст поста...")
        post_text = await generate_post_text(full_description)

        user_posts[message.from_user.id] = {
            'media_path': tmp_path,
            'media_type': 'photo',
            'text': post_text
        }

        await processing_msg.delete()
        await send_draft(message, message.from_user.id)
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        try:
            await processing_msg.edit_text("❌ Что-то пошло не так. Попробуйте ещё раз.")
        except:
            pass
        # Очистка временного файла при ошибке
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

@dp.callback_query(F.data.startswith("edit_req_"))
async def edit_request_callback(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[2])
    user_data = user_posts.get(user_id)
    if not user_data:
        await callback.message.answer("❌ Данные потеряны, начните заново с /start")
        await callback.answer()
        return
    await callback.message.answer(
        "✏️ Напишите, что нужно изменить в тексте.\n"
        "Примеры: «сделай текст короче», «добавь больше эмодзи», «измени тон на более официальный», «напиши от первого лица».\n\n"
        "Пришлите ваш запрос одним сообщением."
    )
    await state.set_state(PostCreation.waiting_for_edit_request)
    await state.update_data(user_id=user_id)
    await callback.answer()

@dp.message(PostCreation.waiting_for_edit_request, F.text)
async def process_edit_request(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get('user_id')
    if not user_id or user_id != message.from_user.id:
        await message.answer("❌ Ошибка, попробуйте заново через /start")
        await state.clear()
        return

    user_data = user_posts.get(user_id)
    if not user_data or not user_data.get('text'):
        await message.answer("❌ Данные потеряны, начните заново.")
        await state.clear()
        return

    current_text = user_data['text']
    edit_request = message.text

    processing_msg = await message.answer("🔄 Редактирую текст по вашему запросу...")
    new_text = await edit_post_text(current_text, edit_request)
    if not new_text:
        await processing_msg.edit_text("❌ Не удалось отредактировать текст. Попробуйте ещё раз.")
        await state.clear()
        return

    user_data['text'] = new_text
    user_posts[user_id] = user_data

    await processing_msg.delete()
    await send_draft(message, user_id)
    await state.clear()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_callback(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    user_data = user_posts.get(user_id)
    if not user_data or not user_data.get('text'):
        await callback.message.answer("❌ Ошибка, начните заново.")
        await callback.answer()
        return

    await callback.message.answer("🚀 Публикую пост в Telegram и VK...")
    success = await publish_to_all(
        user_data['text'],
        user_data.get('media_path'),
        user_data.get('media_type')
    )
    if success:
        await callback.message.answer(
            "✅ Пост опубликован!\n"
            "• Telegram — отправлен\n"
            "• VK — на стене\n"
            "• Дзен — появится автоматически, если настроен кросс-постинг"
        )
    else:
        await callback.message.answer("⚠️ Ошибка при публикации (проверьте логи)")

    # Очистка временного файла
    if user_data.get('media_path') and os.path.exists(user_data['media_path']):
        os.remove(user_data['media_path'])
    user_posts.pop(user_id, None)
    await callback.answer()

# ==================== ЗАПУСК ====================
async def main():
    print("✅ Бот запущен (анализ фото + редактирование через ИИ + публикация)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())