
from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from telegram.ext import Application

from .bot_ext import install as install_bot, startup as bot_startup
from .webapp import install_web


def install_studio(
    api: FastAPI,
    bot_app: Application,
    helpers: dict[str, Any],
    public_url: str,
    bot_token: str,
) -> None:
    install_web(api, bot_token, bot_app.bot)
    install_bot(bot_app, helpers, public_url)


async def studio_startup(bot_app: Application, public_url: str) -> None:
    await bot_startup(bot_app, public_url)
