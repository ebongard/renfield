# Hook Events Reference

All hook events with their kwargs and usage examples.

## Lifecycle Events

### `startup`
- **kwargs:** `app` (FastAPI instance)
- **Execution:** Awaited during startup (after all core services)
- **Use:** Initialize extension services, DB connections

```python
async def _on_startup(app, **kw):
    app.state.my_service = MyService()
    await app.state.my_service.initialize()
```

### `shutdown`
- **kwargs:** `app` (FastAPI instance)
- **Execution:** Awaited before MCP shutdown
- **Use:** Clean up resources, close connections

```python
async def _on_shutdown(app, **kw):
    await app.state.my_service.close()
```

### `register_routes`
- **kwargs:** `app` (FastAPI instance)
- **Execution:** Awaited during startup
- **Use:** Mount additional API routers

```python
async def _on_register_routes(app, **kw):
    from my_plugin.routes import router
    app.include_router(router, prefix="/api/my-plugin")
```

### `register_tools`
- **kwargs:** `registry` (AgentToolRegistry instance)
- **Execution:** Background task via `create_task`
- **Use:** Add custom tools to the Agent Loop

```python
async def _on_register_tools(registry, **kw):
    registry.register("my_tool", my_tool_func, "Description of my tool")
```

## Message Events

### `post_message`
- **kwargs:** `user_msg` (str), `assistant_msg` (str), `user_id` (int|None), `session_id` (str|None)
- **Execution:** Fire-and-forget background task
- **Use:** Post-processing (analytics, KG extraction, logging)

```python
async def _on_post_message(user_msg, assistant_msg, user_id, session_id, **kw):
    await extract_knowledge(user_msg, assistant_msg, user_id)
```

### `post_document_ingest`
- **kwargs:** `chunks` (list), `document_id` (str), `user_id` (int|None)
- **Execution:** Fire-and-forget after RAG ingest or chat upload
- **Use:** KG extraction from document chunks

```python
async def _on_post_document_ingest(chunks, document_id, user_id, **kw):
    for chunk in chunks:
        await process_chunk(chunk, user_id)
```

### `retrieve_context`
- **kwargs:** `query` (str), `user_id` (int|None), `lang` (str)
- **Execution:** Awaited, results appended to memory context
- **Return:** `str` (context to inject) or `None` (skip)

```python
async def _on_retrieve_context(query, user_id, lang, **kw):
    triples = await find_relevant_triples(query, user_id)
    if triples:
        return "## Graph Context\n" + "\n".join(f"- {t}" for t in triples)
    return None
```

## Presence Events

### `presence_enter_room`
- **kwargs:** `user_id` (int), `user_name` (str), `room_id` (int), `room_name` (str), `confidence` (float)

### `presence_leave_room`
- **kwargs:** `user_id` (int), `user_name` (str), `room_id` (int), `room_name` (str)

### `presence_first_arrived`
- **kwargs:** `user_id` (int), `user_name` (str), `room_id` (int), `room_name` (str)

### `presence_last_left`
- **kwargs:** `room_id` (int), `room_name` (str)

```python
async def _on_enter(user_id, user_name, room_id, room_name, confidence, **kw):
    logger.info(f"{user_name} entered {room_name} (confidence: {confidence})")

async def _on_last_left(room_id, room_name, **kw):
    await turn_off_lights(room_id)
```

## Auth Events

### `post_login_granted`
- **kwargs:** `user_id` (int), `provider_id` (str | None — `ldap`, `db`, `legacy_hook`, or the redirect provider id), `channel` (str)
- Fires **once per granted login**, after every check: password login before the tokens are minted; the voice
  second factor after both factors passed (with the FIRST factor's provider); the redirect callback after the
  hand-off code is stored (redeeming the code at `/sso/exchange` does not fire again). Never on a refusal, never
  while a second factor is still pending.
- A **notification, not a gate**: the return value is ignored and a raising handler does not undo the login.
  Identity resolution stays in `post_authenticate`.

```python
async def _on_login(user_id, provider_id, channel, **kw):
    if provider_id == "keycloak":
        await refresh_user_tokens(user_id)
```
