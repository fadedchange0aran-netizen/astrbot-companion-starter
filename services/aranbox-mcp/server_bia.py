from server_core import *  # noqa: F401,F403


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        create_http_app(),
        host=SETTINGS.bind_host,
        port=SETTINGS.bind_port,
    )
