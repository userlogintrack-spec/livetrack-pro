from django.core import signing


WS_TOKEN_MAX_AGE_SECONDS = 60 * 60 * 12
WS_TOKEN_SALT = "chat-ws-token"


def create_ws_token(room_id, role, principal):
    payload = {
        "room_id": str(room_id),
        "role": str(role),
        "principal": str(principal),
    }
    return signing.dumps(payload, salt=WS_TOKEN_SALT)


def verify_ws_token(token, max_age=WS_TOKEN_MAX_AGE_SECONDS):
    try:
        return signing.loads(token, salt=WS_TOKEN_SALT, max_age=max_age)
    except signing.BadSignature:
        return None
