"""
Tasks API Routes
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime

from services.database import get_db
from models.database import Task

router = APIRouter()

class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    task_type: str
    parameters: Dict
    priority: int = 0

class TaskUpdate(BaseModel):
    status: Optional[str] = None
    result: Optional[Dict] = None
    error: Optional[str] = None

@router.post("/create")
async def create_task(
    task: TaskCreate,
    db: AsyncSession = Depends(get_db)
):
    """Neue Aufgabe erstellen"""
    try:
        new_task = Task(
            title=task.title,
            description=task.description,
            task_type=task.task_type,
            parameters=task.parameters,
            priority=task.priority,
            status="pending"
        )
        db.add(new_task)
        await db.commit()
        await db.refresh(new_task)
        
        logger.info(f"✅ Task erstellt: {new_task.id} - {new_task.title}")
        
        return {
            "id": new_task.id,
            "title": new_task.title,
            "status": new_task.status
        }
    except Exception as e:
        logger.error(f"❌ Task Creation Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/list")
async def list_tasks(
    status: Optional[str] = None,
    task_type: Optional[str] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    """Aufgaben auflisten"""
    try:
        query = select(Task)
        
        if status:
            query = query.where(Task.status == status)
        if task_type:
            query = query.where(Task.task_type == task_type)
        
        query = query.order_by(Task.created_at.desc()).limit(limit)
        
        result = await db.execute(query)
        tasks = result.scalars().all()
        
        return {
            "tasks": [
                {
                    "id": task.id,
                    "title": task.title,
                    "task_type": task.task_type,
                    "status": task.status,
                    "created_at": task.created_at.isoformat(),
                    "completed_at": task.completed_at.isoformat() if task.completed_at else None
                }
                for task in tasks
            ]
        }
    except Exception as e:
        logger.error(f"❌ Task List Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{task_id}")
async def get_task(
    task_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Einzelne Aufgabe abrufen"""
    try:
        result = await db.execute(
            select(Task).where(Task.id == task_id)
        )
        task = result.scalar_one_or_none()
        
        if not task:
            raise HTTPException(status_code=404, detail="Task nicht gefunden")
        
        return {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "task_type": task.task_type,
            "status": task.status,
            "parameters": task.parameters,
            "result": task.result,
            "error": task.error,
            "created_at": task.created_at.isoformat(),
            "completed_at": task.completed_at.isoformat() if task.completed_at else None
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Get Task Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/{task_id}")
async def update_task(
    task_id: int,
    update: TaskUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Task aktualisieren"""
    try:
        result = await db.execute(
            select(Task).where(Task.id == task_id)
        )
        task = result.scalar_one_or_none()
        
        if not task:
            raise HTTPException(status_code=404, detail="Task nicht gefunden")
        
        if update.status:
            task.status = update.status
            if update.status == "completed":
                task.completed_at = datetime.utcnow()
        
        if update.result:
            task.result = update.result
        
        if update.error:
            task.error = update.error
        
        await db.commit()
        
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Update Task Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{task_id}")
async def delete_task(
    task_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Task löschen"""
    try:
        result = await db.execute(
            select(Task).where(Task.id == task_id)
        )
        task = result.scalar_one_or_none()
        
        if task:
            await db.delete(task)
            await db.commit()
        
        return {"success": True}
    except Exception as e:
        logger.error(f"❌ Delete Task Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))
