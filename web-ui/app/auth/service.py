import bcrypt as _bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.config import settings

_serializer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="session")


def hash_password(plain: str) -> str:
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode(), hashed.encode())


def create_session_token(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id})


def decode_session_token(token: str) -> int | None:
    try:
        data = _serializer.loads(token, max_age=settings.SESSION_MAX_AGE)
        return data["uid"]
    except (BadSignature, SignatureExpired, KeyError):
        return None
