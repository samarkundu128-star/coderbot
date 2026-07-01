```python
from typing import List, Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import update, delete
from src.database.models import User, Chat, ConversationHistory, Project, GeneratedFile, UsageStatistic

# Base class jo database ke session ko handle karegi
class BaseRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

# UserRepository - Users save karne aur check karne ke functions
class UserRepository(BaseRepository):
    async def get_by_id(self, telegram_id: int) -> Optional[User]:
        stmt = select(User).where(User.telegram_id == telegram_id).options(selectinload(User.stats))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_user(self, telegram_id: int, first_name: str, last_name: Optional[str] = None, username: Optional[str] = None) -> User:
        user = User(
            telegram_id=telegram_id,
            first_name=first_name,
            last_name=last_name,
            username=username
        )
        self.session.add(user)
        # Sath me user ki stats table bhi initialize kar rahe hain
        stats = UsageStatistic(telegram_id=telegram_id)
        self.session.add(stats)
        await self.session.flush() # Memory state push kar rahe hain database me commit se pehle
        return user

# ChatRepository - Chat history save aur check karne ke functions
class ChatRepository(BaseRepository):
    async def get_by_id(self, chat_id: int) -> Optional[Chat]:
        stmt = select(Chat).where(Chat.chat_id == chat_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_chat(self, chat_id: int, telegram_id: int) -> Chat:
        chat = Chat(chat_id=chat_id, telegram_id=telegram_id)
        self.session.add(chat)
        await self.session.flush()
        return chat

    async def get_history(self, chat_id: int, limit: int = 20) -> List[ConversationHistory]:
        stmt = (
            select(ConversationHistory)
            .where(ConversationHistory.chat_id == chat_id)
            .order_by(ConversationHistory.timestamp.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(reversed(result.scalars().all()))

    async def add_history(self, chat_id: int, role: str, content: str) -> ConversationHistory:
        history_entry = ConversationHistory(chat_id=chat_id, role=role, content=content)
        self.session.add(history_entry)
        await self.session.flush()
        return history_entry

    async def clear_history(self, chat_id: int) -> None:
        stmt = delete(ConversationHistory).where(ConversationHistory.chat_id == chat_id)
        await self.session.execute(stmt)

# ProjectRepository - Users ke files/codes database me store karne ke liye
class ProjectRepository(BaseRepository):
    async def create_project(self, telegram_id: int, name: str, description: Optional[str] = None) -> Project:
        project = Project(telegram_id=telegram_id, name=name, description=description)
        self.session.add(project)
        await self.session.flush()
        return project

    async def get_user_projects(self, telegram_id: int) -> List[Project]:
        stmt = select(Project).where(Project.telegram_id == telegram_id).order_by(Project.updated_at.desc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_project_with_files(self, project_id: int) -> Optional[Project]:
        stmt = select(Project).where(Project.id == project_id).options(selectinload(Project.files))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def add_file_to_project(self, project_id: int, file_path: str, content: str) -> GeneratedFile:
        gen_file = GeneratedFile(project_id=project_id, file_path=file_path, content=content)
        self.session.add(gen_file)
        await self.session.flush()
        return gen_file

```
          
