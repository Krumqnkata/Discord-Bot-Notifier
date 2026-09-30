"""Start the existing notifier and optional club commands in one process."""
from contextlib import asynccontextmanager

import uvicorn

import main
from club_fun import install

fun = install(main.bot, main.tree)
app = main.app
notifier_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(app):
    async with notifier_lifespan(app):
        try:
            yield
        finally:
            await fun.close()


app.router.lifespan_context = lifespan

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
