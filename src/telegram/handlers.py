python
import structlog
from telegram import Update, constants
from telegram.ext import CallbackContext
from src.telegram.middleware import TelegramMiddlewareEngine
from src.database.connection import AsyncSessionLocal
from src.database.repository import ChatRepository, ProjectRepository
from src.services.ai_engine import AICodingEngine

logger = structlog.get_logger(__name__)
ai_engine = AICodingEngine()

async def process_ai_request(update: Update, context: CallbackContext, prompt: str) -> None:
    """
    AI model ke paas request bhejta hai aur output telegram par respond karta hai.
    """
    chat_id = update.effective_chat.id
    tg_id = update.effective_user.id
    
    # Telegram typing indicator show karenge jab tak response aa raha hai
    await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
    
    async with AsyncSessionLocal() as session:
        chat_repo = ChatRepository(session)
        proj_repo = ProjectRepository(session)
        
        # Purani conversation history context ke liye fetch karte hain
        raw_history = await chat_repo.get_history(chat_id, limit=10)
        formatted_history = [{"role": h.role, "content": h.content} for h in raw_history]
        
        try:
            # AI engine se output request kar rahe hain
            response_payload = await ai_engine.generate_solution(prompt, formatted_history)
        except Exception as e:
            logger.error("AI engine call failed!", error=str(e))
            await update.effective_chat.send_message(text=f"❌ **Error:** Processing me dikkat aayi: {str(e)}")
            return
        
        files = response_payload.get("files", [])
        commentary = response_payload.get("commentary", "Request successfully processed.")
        
        # Conversation history update kar rahe hain
        await chat_repo.add_history(chat_id, "user", prompt)
        await chat_repo.add_history(chat_id, "assistant", commentary)
        
        # Agar user ka naya project space set hai toh files automatically wahan save hongi
        user_projects = await proj_repo.get_user_projects(tg_id)
        if user_projects and files:
            target_project = user_projects[0]
            for file_info in files:
                await proj_repo.add_file_to_project(
                    project_id=target_project.id,
                    file_path=file_info["file_path"],
                    content=file_info["content"]
                )
        await session.commit()

    # Agar explanation text 4000 characters se bada hai toh break karke bhejenge
    if len(commentary) > 4000:
        for chunk in [commentary[i:i+4000] for i in range(0, len(commentary), 4000)]:
            await update.effective_chat.send_message(text=chunk)
    else:
        await update.effective_chat.send_message(text=commentary)

    # Saari generated codes aur files ko complete formatted codeblock me bhej rahe hain
    for file_info in files:
        escaped_code = f"📂 **File Path:** `{file_info['file_path']}`\n```\n{file_info['content']}\n```"
        await update.effective_chat.send_message(text=escaped_code, parse_mode=constants.ParseMode.MARKDOWN)

async def core_message_handler(update: Update, context: CallbackContext) -> None:
    """
    Normal direct text messages handle karne ke liye middleware checks ke sath.
    """
    if not await TelegramMiddlewareEngine.process_user_and_rate_limit(update, context):
        return
    prompt = update.message.text
    await process_ai_request(update, context, prompt)

async def do_command_handler(update: Update, context: CallbackContext) -> None:
    """
    Special command `/Do` trigger handle karne ke liye.
    """
    if not await TelegramMiddlewareEngine.process_user_and_rate_limit(update, context):
        return
    if not context.args:
        await update.effective_chat.send_message(text="❌ **Instruction empty:** Kripya instruction likhein. E.g., `/Do print hello world`")
        return
    prompt = " ".join(context.args)
    await process_ai_request(update, context, prompt)
