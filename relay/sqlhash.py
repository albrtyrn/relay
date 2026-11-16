"""The one hash function (DESIGN.md, conventions).

The hash is taken over the exact stored text: no whitespace or case normalization (D11).
"Approved this exact statement" means byte for byte.
"""

import hashlib


def sha256_hex(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()
