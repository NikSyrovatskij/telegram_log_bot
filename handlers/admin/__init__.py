from aiogram import Router

from . import (
    client,
    settings,
    users,
    search,
    chats,
    broadcast,
    export,
    archive,
    stats,
)

router = Router()

router.include_router(client.router)
router.include_router(settings.router)
router.include_router(users.router)
router.include_router(search.router)
router.include_router(chats.router)
router.include_router(broadcast.router)
router.include_router(export.router)
router.include_router(archive.router)
router.include_router(stats.router)

__all__ = ["router"]

