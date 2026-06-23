import uvicorn

from server_core import SETTINGS, create_http_app


if __name__ == "__main__":
    uvicorn.run(
        create_http_app(),
        host=SETTINGS.bind_host,
        port=SETTINGS.bind_port,
    )
