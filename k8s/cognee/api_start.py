"""Start Cognee's API server: the container "api" of the pod wattson-cognee (k8s/cognee/cognee.yaml).

This process is the only one that opens the stores on the volume (SQLite, LanceDB, Ladybug).
The MCP container and the web UI reach them only through this server's HTTP API.

Before it serves, it stores the API key of the MCP container: the value of the environment
variable COGNEE_MCP_API_KEY (from the Secret wattson-cognee) becomes the API key named
"wattson-mcp" of Cognee's default user, the owner of the memory. The same key in the Secret
gives the same row, so a restart changes nothing; a new key in the Secret replaces the old one.
The key is never printed. Then it serves cognee.api.client:app with uvicorn on
0.0.0.0:HTTP_API_PORT, in the same process and the same event loop.

Cognee's own start (python -m cognee.api.client) would do the same without the key. This file
uses only Cognee's own functions and models (cognee 1.6.5, pinned in uv.lock).
"""

import asyncio
import os

KEY_NAME = "wattson-mcp"


async def store_mcp_key() -> None:
    key = os.environ.get("COGNEE_MCP_API_KEY", "").strip()
    if len(key) < 32:
        raise SystemExit(
            "COGNEE_MCP_API_KEY is missing or shorter than 32 characters (the Secret wattson-cognee). "
            "Run: bash k8s/cognee.sh --apply"
        )

    from sqlalchemy import select

    from cognee.infrastructure.databases.relational import get_relational_engine
    from cognee.modules.users.api_key.hash_api_key import prepare_api_key
    from cognee.modules.users.methods import get_default_user
    from cognee.modules.users.models.UserApiKey import UserApiKey
    from cognee.run_migrations import run_migrations

    # Once per process: the server's own start (its lifespan) finds it done and skips it.
    await run_migrations()
    # Made here on a new volume (with DEFAULT_USER_PASSWORD as its password); on the volume of
    # the earlier cognee-mcp it is the user that owns the memory already.
    user = await get_default_user()
    stored = prepare_api_key(key)

    async with get_relational_engine().get_async_session() as session:
        query = select(UserApiKey).where(UserApiKey.user_id == user.id, UserApiKey.name == KEY_NAME)
        rows = (await session.execute(query)).scalars().all()
        if len(rows) == 1 and rows[0].api_key == stored:
            print(f"The API key {KEY_NAME} of the MCP container is stored already.", flush=True)
            return
        for row in rows:
            await session.delete(row)
        session.add(UserApiKey(user_id=user.id, api_key=stored, label="****", name=KEY_NAME))
        await session.commit()
    print(f"The API key {KEY_NAME} of the MCP container is stored.", flush=True)


async def main() -> None:
    await store_mcp_key()

    import uvicorn

    from cognee.api.client import app

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("HTTP_API_PORT", "8000")),
        log_level="info",
        server_header=False,
        # The pod gives 60 s at a stop; the server's own shutdown then drains its background
        # work (BACKGROUND_DRAIN_TIMEOUT_SECONDS) and closes the stores.
        timeout_graceful_shutdown=20,
    )
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main())
