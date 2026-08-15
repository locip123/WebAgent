"""Name validation used by the embedded semantic schema."""

import re


def validate_underscore_name_format(value: str) -> bool:
    return bool(re.fullmatch(r'[a-z0-9]+(?:_[a-z0-9]+)*', value))
