import re

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
GIT_SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def required_token(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty")
    return normalized


def normalize_decimal_id(value: str, field_name: str) -> str:
    normalized = required_token(value, field_name)
    if not normalized.isdecimal() or int(normalized) < 1:
        raise ValueError(f"{field_name} must be a positive decimal identity")
    return str(int(normalized))


def normalize_git_sha(value: str, field_name: str) -> str:
    normalized = required_token(value, field_name).lower()
    if GIT_SHA_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a Git SHA")
    return normalized


def normalize_optional_git_sha(value: str, field_name: str) -> str:
    normalized = value.strip().lower()
    if normalized and GIT_SHA_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a Git SHA when present")
    return normalized


def normalize_sha256(value: str, field_name: str) -> str:
    normalized = required_token(value, field_name).lower()
    if SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized
