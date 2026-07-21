import re


EMOTION_PREFIX = "emotion://"
EMOTION_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def valid_emotion_slug(value: str) -> bool:
    return EMOTION_SLUG.fullmatch(value) is not None


def emotion_slug(message: str) -> str | None:
    if not message.startswith(EMOTION_PREFIX):
        return None
    slug = message[len(EMOTION_PREFIX) :]
    return slug if valid_emotion_slug(slug) else None
