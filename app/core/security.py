from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

ph = PasswordHasher()

def hash_user_password(password: str) -> str:
    hash_password = ph.hash(password)
    return hash_password

def verify_user_password(password_hash: str, password_attempt: str):
    try:
        return ph.verify(password_hash, password_attempt)
    except VerifyMismatchError:
        return False



