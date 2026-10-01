"""Per-scope rate limiting for slash commands.

discord.py 2.3 exposes slash-command cooldowns as a *parameter* that has to be
declared in every signature and configured through the tree, which is awkward to
wrap and easy to forget. A 60-line limiter is a better trade: the rules live in
one place, exemption lists are explicit, and behaviour is identical on every
2.x release.

Uses ``time.monotonic`` so a wall-clock adjustment (NTP, DST, a laptop resuming
from sleep) can never hand a user a free pass or a permanent lockout.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Hashable
from typing import TypeVar

from core.errors import CommandCooldownError
from core.logging_setup import get_logger

__all__ = ["RateLimit", "RateLimiter", "limiter"]

logger = get_logger("zagrosian.ratelimit")

F = TypeVar("F", bound=Callable[..., object])


class RateLimiter:
    """Sliding-window limiter keyed by an arbitrary hashable scope.

    Example::

        limiter = RateLimiter(max_calls=3, window=30.0, exempt=lambda i: i.user.id in OWNERS)

        @limiter.limit(scope=lambda interaction: interaction.guild_id)
        async def clear(interaction): ...
    """

    __slots__ = ("_hits", "_last_prune", "exempt", "max_calls", "window")

    def __init__(
        self,
        max_calls: int,
        window: float,
        *,
        exempt: Callable[[object], bool] | None = None,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be >= 1")
        if window <= 0:
            raise ValueError("window must be > 0")
        self.max_calls = max_calls
        self.window = window
        self.exempt = exempt
        self._hits: dict[Hashable, deque[float]] = {}
        self._last_prune = time.monotonic()

    def _prune(self, now: float) -> None:
        """Drop fully-expired buckets so the dict cannot grow without bound."""
        if now - self._last_prune < max(self.window, 60.0):
            return
        cutoff = now - self.window
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            self._hits.pop(key, None)
        self._last_prune = now

    def check(self, scope: Hashable, subject: object | None = None) -> None:
        """Consume one slot for ``scope`` or raise :class:`CommandCooldownError`.

        Args:
            scope: What is being limited, typically a guild ID.
            subject: Who triggered it, typically a user ID. The ``exempt``
                predicate is evaluated against the subject when one is given,
                falling back to the scope — so an operator can exempt either
                their account or a whole guild.
        """
        candidate = subject if subject is not None else scope
        if self.exempt is not None and self.exempt(candidate):
            return

        now = time.monotonic()
        self._prune(now)
        cutoff = now - self.window
        hits = self._hits.setdefault(scope, deque())
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self.max_calls:
            wait = self.window - (now - hits[0])
            raise CommandCooldownError(
                f"You are using this command too quickly. "
                f"Try again in **{max(wait, 0.1):.1f}s**."
            )
        hits.append(now)

    def reset(self, scope: Hashable | None = None) -> None:
        """Clear one scope, or every scope when omitted (used by ``/reload``)."""
        if scope is None:
            self._hits.clear()
            self._last_prune = time.monotonic()
        else:
            self._hits.pop(scope, None)

    def limit(self, scope: Callable[[F], Hashable]) -> Callable[[F], F]:
        """Decorator form: derive the scope from the bound ``self`` argument."""

        def decorator(func: F) -> F:
            import functools

            @functools.wraps(func)
            async def wrapper(*args: object, **kwargs: object) -> object:
                interaction = args[1] if len(args) > 1 else kwargs.get("interaction")
                self.check(scope(func), interaction)  # type: ignore[arg-type]
                return await func(*args, **kwargs)  # type: ignore[misc]

            return wrapper  # type: ignore[return-value]

        return decorator

    def __len__(self) -> int:
        return len(self._hits)


#: Shared limiter for the destructive, abuse-prone commands. 5 per 30s per guild.
limiter = RateLimiter(max_calls=5, window=30.0)


class RateLimit:
    """Declarative limiter over several named actions.

    Usage::

        PUNISHMENT_LIMIT = RateLimit(max_calls=5, window=30.0)
        PUNISHMENT_LIMIT.apply("ban", interaction.guild_id)

    Deliberately **synchronous**. The work is a dict lookup and a deque append;
    an ``async def`` here would hand every call site a coroutine that must be
    awaited, and a call site that forgets produces a limiter that silently does
    nothing at all — the worst possible failure mode for abuse protection.
    """

    __slots__ = ("_exempt", "_registry", "max_calls", "window")

    def __init__(
        self,
        max_calls: int,
        window: float,
        *,
        exempt: Callable[[object], bool] | None = None,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be >= 1")
        if window <= 0:
            raise ValueError("window must be > 0")
        self.max_calls = max_calls
        self.window = window
        self._exempt = exempt
        self._registry: dict[str, RateLimiter] = {}

    def _limiter_for(self, action: str) -> RateLimiter:
        if action not in self._registry:
            self._registry[action] = RateLimiter(
                self.max_calls, self.window, exempt=self._exempt
            )
        return self._registry[action]

    def apply(self, action: str, scope: Hashable, subject: object | None = None) -> None:
        """Consume one slot, or raise :class:`CommandCooldownError`."""
        self._limiter_for(action).check(scope, subject)

    def reset(self) -> None:
        self._registry.clear()
