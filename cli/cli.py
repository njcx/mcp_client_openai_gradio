import aiosqlite
import os
from pathlib import Path


class ConversationManager:
    """Simple conversation manager to store and retrieve conversation IDs"""

    def __init__(self, db_path: Path):
        """Initialize the conversation manager with a database path"""
        self.db_path = db_path
        self._ensure_db_dir()

    def _ensure_db_dir(self):
        """Ensure the database directory exists"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    async def _init_db(self):
        """Initialize the database if it doesn't exist"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.commit()

    async def save_id(self, thread_id: str):
        """Save a conversation ID to the database"""
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO conversations (id) VALUES (?)", (thread_id,))
            await db.commit()

    async def get_last_id(self) -> str:
        """Get the last conversation ID from the database"""
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT id FROM conversations ORDER BY timestamp DESC LIMIT 1")
            result = await cursor.fetchone()
            return result[0] if result else None
