"""
Task Queue Service mit Redis (async)
"""
import json

import redis.asyncio as aioredis
from loguru import logger

from utils.config import settings


class TaskQueue:
    """Async Task Queue mit Redis"""

    def __init__(self):
        self.redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        self.queue_name = "renfield:tasks"

    async def enqueue(self, task_type: str, parameters: dict) -> str:
        """Task in Queue einreihen"""
        try:
            task_id = f"task:{task_type}:{await self.redis_client.incr('task:counter')}"

            task_data = {
                "id": task_id,
                "type": task_type,
                "parameters": parameters,
                "status": "queued"
            }

            # In Redis speichern
            await self.redis_client.lpush(self.queue_name, json.dumps(task_data))
            await self.redis_client.set(task_id, json.dumps(task_data))

            logger.info(f"Task {task_id} eingefuegt")
            return task_id
        except Exception as e:
            logger.error(f"Enqueue Fehler: {e}")
            raise

    async def dequeue(self) -> dict | None:
        """Naechsten Task aus Queue holen"""
        try:
            task_json = await self.redis_client.rpop(self.queue_name)
            if task_json:
                return json.loads(task_json)
            return None
        except Exception as e:
            logger.error(f"Dequeue Fehler: {e}")
            return None

    async def get_task_status(self, task_id: str) -> dict | None:
        """Task-Status abrufen"""
        try:
            task_json = await self.redis_client.get(task_id)
            if task_json:
                return json.loads(task_json)
            return None
        except Exception as e:
            logger.error(f"Get Status Fehler: {e}")
            return None

    async def update_task_status(self, task_id: str, status: str, result: dict | None = None):
        """Task-Status aktualisieren"""
        try:
            task = await self.get_task_status(task_id)
            if task:
                task["status"] = status
                if result:
                    task["result"] = result
                await self.redis_client.set(task_id, json.dumps(task))
                logger.info(f"Task {task_id} Status: {status}")
        except Exception as e:
            logger.error(f"Update Status Fehler: {e}")

    async def queue_length(self) -> int:
        """Anzahl der Tasks in Queue"""
        return await self.redis_client.llen(self.queue_name)

    async def close(self):
        """Close Redis connection gracefully."""
        await self.redis_client.close()
