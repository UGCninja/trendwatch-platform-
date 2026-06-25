import os
from itsdangerous import URLSafeTimedSerializer
from fastapi import Request
from fastapi.responses import RedirectResponse

SECRET_KEY   = os.getenv("SECRET_KEY", "trendwatch-secret-2026")
APP_PASSWORD = os.getenv("APP_PASSWORD", "UGC_ninja2026")

_serializer = URLSafeTimedSerializer(SECRET_KEY)


def create_session_token() -> str:
    return _serializer.dumps("ok")


def check_auth(request: Request) -> bool:
    token = request.cookies.get("session")
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=86400 * 30)
        return True
    except Exception:
        return False
