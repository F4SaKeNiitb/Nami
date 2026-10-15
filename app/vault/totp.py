"""TOTP helpers (RFC 6238) on top of pyotp. Seeds are secrets: never log them."""

import re
import time
from dataclasses import dataclass

import pyotp


class InvalidTotp(ValueError):
    pass


@dataclass(frozen=True)
class TotpParams:
    seed: str
    digits: int = 6
    period: int = 30
    algorithm: str = "SHA1"

    @property
    def standard(self) -> bool:
        """6 digits / 30 s / SHA1 is what browser-use can generate itself."""
        return (self.digits, self.period, self.algorithm) == (6, 30, "SHA1")

    def __repr__(self) -> str:  # never print the seed
        return f"TotpParams(digits={self.digits}, period={self.period}, algorithm={self.algorithm})"


def parse(text: str) -> TotpParams:
    """Accept a bare base32 seed (spaces allowed) or an otpauth:// URI."""
    text = text.strip()
    try:
        if text.lower().startswith("otpauth://"):
            t = pyotp.parse_uri(text)
            if not isinstance(t, pyotp.TOTP):
                raise InvalidTotp("only time-based codes are supported")
            algo = {"sha1": "SHA1", "sha256": "SHA256", "sha512": "SHA512"}[t.digest().name]
            params = TotpParams(t.secret, t.digits, t.interval, algo)
        else:
            seed = re.sub(r"[\s-]", "", text).upper()
            params = TotpParams(seed)
        if len(params.seed) < 16:  # 80 bits: the shortest seed real services issue
            raise InvalidTotp("key too short")
        _totp(params).now()  # proves the seed is valid base32
    except InvalidTotp:
        raise
    except Exception:
        raise InvalidTotp("not a valid authenticator key") from None
    return params


def _totp(p: TotpParams) -> pyotp.TOTP:
    import hashlib
    return pyotp.TOTP(p.seed, digits=p.digits, interval=p.period, digest=getattr(hashlib, p.algorithm.lower()))


def code_at(p: TotpParams, at: float | None = None) -> str:
    return _totp(p).at(at if at is not None else time.time())


def seconds_left(p: TotpParams, at: float | None = None) -> float:
    now = at if at is not None else time.time()
    return p.period - (now % p.period)


def verify(p: TotpParams, code: str, at: float | None = None, window: int = 1) -> bool:
    """Accept the current code and `window` steps either side, to tolerate clock drift."""
    return _totp(p).verify(code.strip(), for_time=at if at is not None else time.time(), valid_window=window)


def code_for_fill(p: TotpParams, min_left: float = 5.0, at: float | None = None) -> tuple[str, float]:
    """A code that will still be valid when typed. Returns (code, seconds_to_wait_first).

    If the window is about to roll over, the caller should wait and use the next code.
    """
    now = at if at is not None else time.time()
    left = seconds_left(p, now)
    if left >= min_left:
        return code_at(p, now), 0.0
    return code_at(p, now + left + 0.001), left + 0.001
