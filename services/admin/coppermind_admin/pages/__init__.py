"""The Admin pages, one module for each part of the menu."""

from fastapi import FastAPI

from coppermind.settings import Wiring


def register(app: FastAPI, wiring: Wiring) -> None:
    from coppermind_admin.pages import (
        apps,
        calendar,
        features,
        home,
        incoming,
        notes,
        people,
        services,
        settings,
        storage,
        sync,
    )

    for module in (
        home,
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
