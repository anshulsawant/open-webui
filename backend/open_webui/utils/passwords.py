# FILE: open-webui/backend/open_webui/utils/passwords.py
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password) if hashed_password else None

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)
