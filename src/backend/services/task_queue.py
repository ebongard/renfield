"""
Task Queue Service mit Redis
"""
import json

import redis
from loguru import logger

from utils.config import settings


class TaskQueue:
    """Simple Task Queue mit Redis"""

    def __init__(self):
        self.redis_client = redis.from_url(settings.redis_url, decode_responses=True)
        self.queue_name = "renfield:tasks"

    async def enqueue(self, task_type: str, parameters: dict) -> str:
        """Task in Queue einreihen"""
        try:
            task_id = f"task:{task_type}:{self.redis_client.incr('task:counter')}"

            task_data = {
                "id": task_id,
                "type": task_type,
                "parameters": parameters,
                "status": "queued"
            }

            # In Redis speichern
            self.redis_client.lpush(self.queue_name, json.dumps(task_data))
            self.redis_client.set(task_id, json.dumps(task_data))

            logger.info(f"✅ Task {task_id} eingefügt")
            return task_id
        except Exception as e:
            logger.error(f"❌ Enqueue Fehler: {e}")
            raise

    def dequeue(self) -> dict | None:
        """Nächsten Task aus Queue holen"""
        try:
            task_json = self.redis_client.rpop(self.queue_name)
            if task_json:
                return json.loads(task_json)
            return None
        except Exception as e:
            logger.error(f"❌ Dequeue Fehler: {e}")
            return None

    def get_task_status(self, task_id: str) -> dict | None:
        """Task-Status abrufen"""
        try:
            task_json = self.redis_client.get(task_id)
            if task_json:
                return json.loads(task_json)
            return None
        except Exception as e:
            logger.error(f"❌ Get Status Fehler: {e}")
            return None

    def update_task_status(self, task_id: str, status: str, result: dict | None = None):
        """Task-Status aktualisieren"""
        try:
            task = self.get_task_status(task_id)
            if task:
                task["status"] = status
                if result:
                    task["result"] = result
                self.redis_client.set(task_id, json.dumps(task))
                logger.info(f"✅ Task {task_id} Status: {status}")
        except Exception as e:
            logger.error(f"❌ Update Status Fehler: {e}")

    def queue_length(self) -> int:
        """Anzahl der Tasks in Queue"""
        return self.redis_client.llen(self.queue_name)
