"""Local development commands."""

import uvicorn


def start() -> None:
    """Start the development server with automatic reload."""

    uvicorn.run(
        "core_console.app:create_app",
        factory=True,
        reload=True,
        access_log=False,
    )
