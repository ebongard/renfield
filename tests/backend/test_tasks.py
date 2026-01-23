"""
Tests für Tasks API

Testet:
- Task CRUD Operations
- Task Status Updates
- Task Filtering
"""

import pytest
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from httpx import AsyncClient

from models.database import Task


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def sample_task_data():
    """Sample task data for tests"""
    return {
        "title": "Test Task",
        "description": "A test task for unit tests",
        "task_type": "test_type",
        "parameters": {"key": "value"},
        "priority": 1
    }


@pytest.fixture
async def test_task(db_session: AsyncSession, sample_task_data) -> Task:
    """Create a test task in database"""
    task = Task(
        **sample_task_data,
        status="pending"
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.fixture
async def completed_task(db_session: AsyncSession) -> Task:
    """Create a completed task in database"""
    task = Task(
        title="Completed Task",
        task_type="completed_type",
        parameters={},
        status="completed",
        result={"output": "success"},
        completed_at=datetime.utcnow()
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


# ============================================================================
# Model Tests
# ============================================================================

class TestTaskModel:
    """Tests für das Task Model"""

    @pytest.mark.database
    async def test_create_task(self, db_session: AsyncSession, sample_task_data):
        """Testet das Erstellen eines Tasks"""
        task = Task(**sample_task_data, status="pending")
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        assert task.id is not None
        assert task.title == sample_task_data["title"]
        assert task.status == "pending"
        assert task.created_at is not None

    @pytest.mark.database
    async def test_task_with_result(self, db_session: AsyncSession):
        """Testet Task mit Ergebnis"""
        task = Task(
            title="Task with Result",
            task_type="result_type",
            parameters={},
            status="completed",
            result={"data": [1, 2, 3]}
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        assert task.result is not None
        assert task.result["data"] == [1, 2, 3]

    @pytest.mark.database
    async def test_task_with_error(self, db_session: AsyncSession):
        """Testet Task mit Fehler"""
        task = Task(
            title="Failed Task",
            task_type="error_type",
            parameters={},
            status="failed",
            error="Something went wrong"
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        assert task.error == "Something went wrong"
        assert task.status == "failed"


# ============================================================================
# CRUD API Tests
# ============================================================================

class TestTaskCRUDAPI:
    """Tests für Task CRUD API"""

    @pytest.mark.integration
    async def test_create_task_endpoint(
        self,
        async_client: AsyncClient,
        sample_task_data
    ):
        """Testet POST /api/tasks/create"""
        response = await async_client.post(
            "/api/tasks/create",
            json=sample_task_data
        )

        assert response.status_code == 200
        data = response.json()
        assert data["title"] == sample_task_data["title"]
        assert data["status"] == "pending"
        assert "id" in data

    @pytest.mark.integration
    async def test_list_tasks(
        self,
        async_client: AsyncClient,
        test_task: Task
    ):
        """Testet GET /api/tasks/list"""
        response = await async_client.get("/api/tasks/list")

        assert response.status_code == 200
        data = response.json()
        assert "tasks" in data
        assert isinstance(data["tasks"], list)

    @pytest.mark.integration
    async def test_list_tasks_with_status_filter(
        self,
        async_client: AsyncClient,
        test_task: Task,
        completed_task: Task
    ):
        """Testet GET /api/tasks/list mit Status-Filter"""
        response = await async_client.get("/api/tasks/list?status=pending")

        assert response.status_code == 200
        data = response.json()
        assert all(t["status"] == "pending" for t in data["tasks"])

    @pytest.mark.integration
    async def test_list_tasks_with_type_filter(
        self,
        async_client: AsyncClient,
        test_task: Task
    ):
        """Testet GET /api/tasks/list mit Typ-Filter"""
        response = await async_client.get(f"/api/tasks/list?task_type={test_task.task_type}")

        assert response.status_code == 200
        data = response.json()
        assert all(t["task_type"] == test_task.task_type for t in data["tasks"])

    @pytest.mark.integration
    async def test_list_tasks_with_limit(
        self,
        async_client: AsyncClient,
        test_task: Task
    ):
        """Testet GET /api/tasks/list mit Limit"""
        response = await async_client.get("/api/tasks/list?limit=5")

        assert response.status_code == 200
        data = response.json()
        assert len(data["tasks"]) <= 5

    @pytest.mark.integration
    async def test_get_task(
        self,
        async_client: AsyncClient,
        test_task: Task
    ):
        """Testet GET /api/tasks/{task_id}"""
        response = await async_client.get(f"/api/tasks/{test_task.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == test_task.id
        assert data["title"] == test_task.title
        assert data["parameters"] == test_task.parameters

    @pytest.mark.integration
    async def test_get_nonexistent_task(self, async_client: AsyncClient):
        """Testet GET für nicht-existenten Task"""
        response = await async_client.get("/api/tasks/99999")

        assert response.status_code == 404

    @pytest.mark.integration
    async def test_update_task_status(
        self,
        async_client: AsyncClient,
        test_task: Task
    ):
        """Testet PATCH /api/tasks/{task_id} - Status Update"""
        response = await async_client.patch(
            f"/api/tasks/{test_task.id}",
            json={"status": "in_progress"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    @pytest.mark.integration
    async def test_update_task_completed(
        self,
        async_client: AsyncClient,
        test_task: Task
    ):
        """Testet PATCH /api/tasks/{task_id} - Completion"""
        response = await async_client.patch(
            f"/api/tasks/{test_task.id}",
            json={
                "status": "completed",
                "result": {"output": "finished"}
            }
        )

        assert response.status_code == 200

    @pytest.mark.integration
    async def test_update_task_with_error(
        self,
        async_client: AsyncClient,
        test_task: Task
    ):
        """Testet PATCH /api/tasks/{task_id} - Error"""
        response = await async_client.patch(
            f"/api/tasks/{test_task.id}",
            json={
                "status": "failed",
                "error": "Task execution failed"
            }
        )

        assert response.status_code == 200

    @pytest.mark.integration
    async def test_update_nonexistent_task(self, async_client: AsyncClient):
        """Testet PATCH für nicht-existenten Task"""
        response = await async_client.patch(
            "/api/tasks/99999",
            json={"status": "completed"}
        )

        assert response.status_code == 404

    @pytest.mark.integration
    async def test_delete_task(
        self,
        async_client: AsyncClient,
        test_task: Task
    ):
        """Testet DELETE /api/tasks/{task_id}"""
        response = await async_client.delete(f"/api/tasks/{test_task.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

        # Verify deleted
        response = await async_client.get(f"/api/tasks/{test_task.id}")
        assert response.status_code == 404

    @pytest.mark.integration
    async def test_delete_nonexistent_task(self, async_client: AsyncClient):
        """Testet DELETE für nicht-existenten Task (idempotent)"""
        response = await async_client.delete("/api/tasks/99999")

        # Should succeed (idempotent)
        assert response.status_code == 200


# ============================================================================
# Query Tests
# ============================================================================

class TestTaskQueries:
    """Tests für Task-Abfragen"""

    @pytest.mark.database
    async def test_filter_by_status(
        self,
        db_session: AsyncSession,
        test_task: Task
    ):
        """Testet Filterung nach Status"""
        result = await db_session.execute(
            select(Task).where(Task.status == "pending")
        )
        tasks = result.scalars().all()

        assert len(tasks) >= 1
        assert all(t.status == "pending" for t in tasks)

    @pytest.mark.database
    async def test_filter_by_task_type(
        self,
        db_session: AsyncSession,
        test_task: Task
    ):
        """Testet Filterung nach Task-Typ"""
        result = await db_session.execute(
            select(Task).where(Task.task_type == test_task.task_type)
        )
        tasks = result.scalars().all()

        assert len(tasks) >= 1
        assert all(t.task_type == test_task.task_type for t in tasks)

    @pytest.mark.database
    async def test_order_by_created_at(
        self,
        db_session: AsyncSession,
        test_task: Task
    ):
        """Testet Sortierung nach Erstellungsdatum"""
        result = await db_session.execute(
            select(Task).order_by(Task.created_at.desc())
        )
        tasks = result.scalars().all()

        # Should be sorted descending
        for i in range(1, len(tasks)):
            assert tasks[i-1].created_at >= tasks[i].created_at


# ============================================================================
# Edge Cases
# ============================================================================

class TestTaskEdgeCases:
    """Tests für Edge Cases"""

    @pytest.mark.integration
    async def test_create_task_minimal_data(self, async_client: AsyncClient):
        """Testet Task-Erstellung mit minimalen Daten"""
        response = await async_client.post(
            "/api/tasks/create",
            json={
                "title": "Minimal Task",
                "task_type": "minimal",
                "parameters": {}
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Minimal Task"

    @pytest.mark.integration
    async def test_create_task_with_priority(self, async_client: AsyncClient):
        """Testet Task-Erstellung mit Priorität"""
        response = await async_client.post(
            "/api/tasks/create",
            json={
                "title": "High Priority Task",
                "task_type": "priority",
                "parameters": {},
                "priority": 10
            }
        )

        assert response.status_code == 200

    @pytest.mark.database
    async def test_task_parameters_json(self, db_session: AsyncSession):
        """Testet komplexe JSON-Parameter"""
        complex_params = {
            "nested": {"key": "value"},
            "list": [1, 2, 3],
            "string": "test"
        }

        task = Task(
            title="Complex Params",
            task_type="complex",
            parameters=complex_params,
            status="pending"
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        assert task.parameters == complex_params
        assert task.parameters["nested"]["key"] == "value"
