"""The rule of a new password: ONE rule for every place a password is set.

The bootstrap administrator (db/seed.py, through config.admin_password_problem),
user creation and an administrator's change (routes/users.py, through the
`NewPassword` type of schemas/models.py) and one's own change from the settings
menu all ask `password_problem`. The interface reads the same numbers and list
from GET /users/password-rules, so it says what is wrong before asking.

It applies when a password is SET: a password chosen under an older rule keeps
signing in (and can be changed into a good one).
"""
from typing import Optional

PASSWORD_MIN_LENGTH = 12
# bcrypt reads at most 72 bytes: a longer password would be cut silently, or
# refused by the library with a 500
PASSWORD_MAX_BYTES = 72

# Passwords published somewhere: the old code default and example settings, the
# getting-started guide's examples, the usual first guesses. Compared without
# case and surrounding spaces.
KNOWN_PASSWORDS = frozenset({
    "admin", "administrator", "change-me-in-production", "change-me-2026", "changeme", "change-me",
    "password", "tabularia", "admin123", "12345678", "123456789012",
})


def is_known_password(password: str) -> bool:
    return password.strip().lower() in KNOWN_PASSWORDS


def password_problem(password: str) -> Optional[str]:
    """Why this cannot be a new password, or None if it can (English: the gateway's
    messages are; the interface translates from the rule, not from this text)."""
    if not password:
        return "the password is empty"
    if is_known_password(password):
        return "the password is a published one everyone can try"
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"the password is too short ({len(password)} characters, at least {PASSWORD_MIN_LENGTH} are needed)"
    if len(password.encode()) > PASSWORD_MAX_BYTES:
        return f"the password is longer than {PASSWORD_MAX_BYTES} bytes, more than bcrypt reads"
    return None
