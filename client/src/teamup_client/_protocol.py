import base64
import binascii
import json
import re
from uuid import UUID

REQUEST_LIMIT = 128 * 1024
RESPONSE_LIMIT = 16 * 1024 * 1024


def _reject_constant(_value):
    raise ValueError("Non-finite numbers are invalid JSON")


def _unique_keys(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def decode_json(data):
    return json.loads(
        data.decode("utf-8"),
        object_pairs_hook=_unique_keys,
        parse_constant=_reject_constant,
    )


def frame(data):
    return (json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n").encode(
        "utf-8"
    )


def is_secret(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        return False
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except binascii.Error:
        return False
    return base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") == value


def is_string(value, limit=None, *, require_content=True):
    if not isinstance(value, str):
        return False
    if (require_content and not value.strip()) or (
        limit is not None and len(value) > limit
    ):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def is_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


def is_integer(value, minimum, maximum=None):
    return (
        type(value) is int
        and value >= minimum
        and (maximum is None or value <= maximum)
    )
