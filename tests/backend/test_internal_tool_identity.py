"""Internal tools take the asker's identity from the SERVER, never from the LLM.

Found on a fresh instance: `ActionExecutor` dispatched `internal.knowledge_search`
without the authenticated `user_id`, and `knowledge_search` read `user_id` from
the LLM-built parameter dict instead. With auth on, the agent's search therefore
ran as "nobody" and reached the public tier only — a user's own documents were
never found (`dense=0, bm25=0` while `rag.search(user_id=2)` found the chunk).
The same read-identity-from-params pattern sat in the three radio-favorite tools
and the Media Follow Me session owner (ha_glue).

Three layers, each with its own test:
- through the dispatch, on real Postgres: own private document found, another
  user's not, a smuggled `user_id` ignored (knowledge search + radio favorites);
- the executor: every `internal.*` branch that reads user data passes `user_id=`
  (AST over the function BODY — prose naming `user_id` must not satisfy it);
- the handlers: none reads an identity key out of its parameter dict.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom, Document, DocumentChunk, KnowledgeBase
from tests.backend.dbrows import ensure_role, ensure_user
from utils.config import settings

BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

TIER_SELF = 0
WORD = "Zaunkoenigsiegel"


# ------------------------------------------------------------ fixtures -------
@pytest.fixture
async def two_users(db_session: AsyncSession, monkeypatch):
    """Two users in no circle of each other, auth ON."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    await ensure_role(db_session)
    alice = await ensure_user(db_session, 1, username="alice")
    bob = await ensure_user(db_session, 2, username="bob")
    await db_session.commit()
    return alice, bob


@pytest.fixture
def bm25_only(monkeypatch):
    """Real retrieval SQL without a model server: the query embedding fails, so
    `RAGRetrieval.search` takes its documented BM25 fallback over the same
    circle filter. Rerank, parent resolution, window expansion and the fact
    path are switched off — none of them decides visibility."""
    from services.rag_retrieval import RAGRetrieval

    async def _no_embedding(self, _text):
        raise RuntimeError("no embedding model in the test")

    monkeypatch.setattr(RAGRetrieval, "get_embedding", _no_embedding)
    monkeypatch.setattr(settings, "rag_rerank_enabled", False)
    monkeypatch.setattr(settings, "rag_parent_child_enabled", False)
    monkeypatch.setattr(settings, "rag_context_window", 0)
    monkeypatch.setattr(settings, "schicht_a_extraction_enabled", False)


async def _private_document(db: AsyncSession, owner_id: int, label: str) -> int:
    """A completed tier-0 document with one chunk carrying WORD; returns its id."""
    kb = KnowledgeBase(name=f"KB {label}", is_active=True, owner_id=owner_id)
    db.add(kb)
    await db.flush()
    atom_id = f"ident-{label}"
    db.add(Atom(atom_id=atom_id, atom_type="kb_document", source_table="documents",
                source_id=f"__pending__{atom_id}", owner_user_id=owner_id,
                policy={"tier": TIER_SELF}))
    await db.flush()
    doc = Document(filename=f"{label}.pdf", file_path=f"/x/{label}.pdf",
                   status="completed", knowledge_base_id=kb.id,
                   circle_tier=TIER_SELF, atom_id=atom_id)
    db.add(doc)
    await db.flush()
    await db.execute(text("UPDATE atoms SET source_id = :s WHERE atom_id = :a"),
                     {"s": str(doc.id), "a": atom_id})
    db.add(DocumentChunk(document_id=doc.id, content=f"{WORD} im Vertrag von {label}",
                         chunk_index=0, circle_tier=TIER_SELF))
    await db.commit()
    return doc.id


async def _search(asker_id: int | None, **smuggled) -> set[int]:
    from services.action_executor import ActionExecutor

    result = await ActionExecutor().execute(
        {"intent": "internal.knowledge_search",
         "parameters": {"query": WORD, **smuggled}, "confidence": 1.0},
        user_id=asker_id,
    )
    assert result["success"] is True, result
    return {s["document_id"] for s in (result.get("data") or {}).get("sources", [])}


# ------------------------------------------- through the dispatch: search ----
@pytest.mark.database
class TestKnowledgeSearchThroughTheDispatch:

    async def test_own_private_document_is_found(
        self, db_session, two_users, bm25_only, own_session_to_test_db
    ):
        alice, _ = two_users
        own = await _private_document(db_session, alice.id, "alice")

        assert own in await _search(alice.id), (
            "the agent's search did not reach the asker's own tier-0 document"
        )

    async def test_another_users_private_document_is_not_found(
        self, db_session, two_users, bm25_only, own_session_to_test_db
    ):
        alice, bob = two_users
        own = await _private_document(db_session, alice.id, "alice")
        foreign = await _private_document(db_session, bob.id, "bob")

        seen = await _search(alice.id)
        assert own in seen
        assert foreign not in seen, "alice reached bob's tier-0 document"

    async def test_a_user_id_smuggled_in_the_params_is_ignored(
        self, db_session, two_users, bm25_only, own_session_to_test_db
    ):
        alice, bob = two_users
        own = await _private_document(db_session, alice.id, "alice")
        foreign = await _private_document(db_session, bob.id, "bob")

        # Authenticated as alice, the model names bob: still alice's reach.
        seen = await _search(alice.id, user_id=bob.id)
        assert own in seen
        assert foreign not in seen, "a user_id in the LLM params widened the search"
        # Unidentified turn (auth on): naming bob must not borrow his reach.
        assert foreign not in await _search(None, user_id=bob.id)


# ------------------------------------ through the dispatch: radio favorites --
@pytest.fixture
def ha_glue_dispatch(monkeypatch):
    """The `execute_tool` hook as ha_glue registers it at startup."""
    from ha_glue.bootstrap import ha_glue_execute_tool
    from utils import hooks

    monkeypatch.setitem(hooks._hooks, "execute_tool", [ha_glue_execute_tool])


async def _radio(intent: str, asker_id: int | None, **params) -> dict:
    from services.action_executor import ActionExecutor

    return await ActionExecutor().execute(
        {"intent": intent, "parameters": params, "confidence": 1.0},
        user_id=asker_id,
    )


@pytest.mark.database
class TestRadioFavoritesThroughTheDispatch:

    async def test_the_favorite_belongs_to_the_authenticated_caller(
        self, db_session, two_users, ha_glue_dispatch, own_session_to_test_db
    ):
        from ha_glue.models.database import RadioFavorite

        alice, bob = two_users
        saved = await _radio("internal.save_radio_favorite", alice.id,
                             station_id="s1", station_name="Eins", user_id=bob.id)
        assert saved["success"] is True, saved

        owners = (await db_session.execute(
            select(RadioFavorite.user_id).where(RadioFavorite.station_id == "s1")
        )).scalars().all()
        assert owners == [alice.id], f"favorite owned by {owners}, not the caller"

    async def test_list_and_remove_never_reach_another_users_favorites(
        self, db_session, two_users, ha_glue_dispatch, own_session_to_test_db
    ):
        from ha_glue.models.database import RadioFavorite

        alice, bob = two_users
        await _radio("internal.save_radio_favorite", bob.id,
                     station_id="s2", station_name="Zwei")

        listed = await _radio("internal.list_radio_favorites", alice.id, user_id=bob.id)
        assert listed["data"]["favorites"] == [], "alice listed bob's favorites"

        removed = await _radio("internal.remove_radio_favorite", alice.id,
                               station_id="s2", user_id=bob.id)
        assert removed["success"] is False
        left = (await db_session.execute(
            select(RadioFavorite.user_id).where(RadioFavorite.station_id == "s2")
        )).scalars().all()
        assert left == [bob.id], "alice removed bob's favorite"


# ----------------------------------------------------- the executor (AST) ----
def _function_body(fn) -> ast.FunctionDef:
    """The function's AST without its docstring (comments are gone already)."""
    node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    node.body = [
        stmt for stmt in node.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
    ]
    return node


def _internal_branches(fn_node: ast.FunctionDef) -> dict[str, ast.If]:
    """Every `if intent == "internal.X":` branch, keyed by the tool name."""
    found: dict[str, ast.If] = {}
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                and test.left.id == "intent" and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and isinstance(test.comparators[0], ast.Constant)
                and str(test.comparators[0].value).startswith("internal.")):
            found[test.comparators[0].value] = node
    return found


def _passes_user_id(branch: ast.If) -> bool:
    """A call in the branch hands the authenticated `user_id` on as `user_id=`."""
    for node in ast.walk(ast.Module(body=branch.body, type_ignores=[])):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (kw.arg == "user_id" and isinstance(kw.value, ast.Name)
                        and kw.value.id == "user_id"):
                    return True
    return False


# Branches that read no user data at all — pure validation of LLM-computed
# rows, or a public weather lookup. Anything else must carry the asker.
NO_USER_DATA = frozenset({
    "internal.render_table", "internal.render_list", "internal.weather_widget",
})


class TestExecutorInjectsTheAsker:

    @pytest.mark.unit
    def test_every_internal_branch_that_reads_user_data_passes_user_id(self):
        from services.action_executor import ActionExecutor

        branches = _internal_branches(_function_body(ActionExecutor.execute))
        assert "internal.knowledge_search" in branches  # the walk sees the code
        missing = sorted(
            name for name, branch in branches.items()
            if name not in NO_USER_DATA and not _passes_user_id(branch)
        )
        assert missing == [], f"internal.* branches without user_id=: {missing}"

    @pytest.mark.unit
    def test_the_exemptions_are_still_real_branches(self):
        """A stale exemption would silently excuse a renamed tool."""
        from services.action_executor import ActionExecutor

        branches = _internal_branches(_function_body(ActionExecutor.execute))
        assert set(NO_USER_DATA) <= set(branches)

    @pytest.mark.unit
    def test_the_generic_internal_hook_carries_the_asker(self):
        """The `execute_tool` hook (ha_glue's tools) gets `user_id=` too."""
        from services.action_executor import ActionExecutor

        body = _function_body(ActionExecutor.execute)
        hook_calls = [
            n for n in ast.walk(body)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "run_hooks"
            and n.args and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == "execute_tool"
        ]
        assert hook_calls
        assert all(
            any(kw.arg == "user_id" and getattr(kw.value, "id", None) == "user_id"
                for kw in call.keywords)
            for call in hook_calls
        )

    @pytest.mark.unit
    def test_identity_keys_are_stripped_from_internal_params(self):
        from services.action_executor import strip_server_injected_keys

        assert strip_server_injected_keys(
            {"query": "q", "user_id": 2, "session_id": "s", "user_permissions": ["admin"]}
        ) == {"query": "q"}


# ------------------------------------------------------ the handlers (AST) ---
IDENTITY_KEYS = {"user_id", "session_id", "user_permissions"}
PARAM_NAMES = {"params", "parameters", "args", "arguments"}


def _identity_reads(path: Path) -> list[str]:
    """`params.get("user_id")` / `params["user_id"]` on a parameter dict."""
    tree = ast.parse(path.read_text())
    hits: list[str] = []
    for node in ast.walk(tree):
        key = None
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"get", "pop"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in PARAM_NAMES
                and node.args and isinstance(node.args[0], ast.Constant)):
            key = node.args[0].value
        elif (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Name) and node.value.id in PARAM_NAMES
                and isinstance(node.slice, ast.Constant)):
            key = node.slice.value
        if key in IDENTITY_KEYS:
            hits.append(f"{path.name}:{node.lineno} {key}")
    return hits


def _handler_modules() -> list[Path]:
    services = BACKEND / "services"
    return sorted(
        [*services.glob("*_tool.py"), services / "widget_tools.py",
         BACKEND / "ha_glue" / "services" / "internal_tools.py"]
    )


class TestHandlersNeverReadIdentityFromParams:

    @pytest.mark.unit
    def test_the_scan_covers_the_known_handlers(self):
        names = {p.name for p in _handler_modules()}
        assert {"knowledge_tool.py", "memory_list_tool.py", "internal_tools.py"} <= names

    @pytest.mark.unit
    def test_the_detector_fires_on_the_pattern(self, tmp_path):
        """Negative control for the scanner itself."""
        probe = tmp_path / "probe_tool.py"
        probe.write_text(
            'def f(params, parameters):\n'
            '    a = params.get("user_id")\n'
            '    b = parameters["session_id"]\n'
            '    return params.get("query"), a, b\n'
        )
        assert len(_identity_reads(probe)) == 2

    @pytest.mark.unit
    def test_no_handler_reads_identity_from_its_params(self):
        hits = [h for p in _handler_modules() for h in _identity_reads(p)]
        assert hits == [], f"identity read from LLM params: {hits}"
