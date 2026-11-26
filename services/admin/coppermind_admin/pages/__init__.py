"""The Admin pages: the Coppermind page and the Advanced page, and what each one opens."""

from fastapi import FastAPI

from coppermind.settings import Wiring


def register(app: FastAPI, wiring: Wiring) -> None:
    from coppermind_admin.pages import (
        advanced,
        ai,
        apps,
        calendar,
        companion,
        coppermind,
        features,
        incoming,
        notes,
        people,
        services,
        settings,
        storage,
        sync,
    )

    for module in (
        coppermind,
        advanced,
        ai,
        companion,
        notes,
        incoming,
        calendar,
        people,
        sync,
        services,
        features,
        apps,
        storage,
        settings,
    ):
        app.include_router(module.router(wiring))
