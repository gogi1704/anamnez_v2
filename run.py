from backend.main import _record_server_error, serve


if __name__ == "__main__":
    try:
        serve()
    except Exception:
        _record_server_error("Сервер не смог запуститься")
        raise
