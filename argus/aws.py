"""One place where every AWS client is built.

Three things every Argus AWS call needs, and gets here rather than in each
module:

* **Adaptive retries.** Argus deploys in parallel, so it hits
  ``describe_stacks`` and ``get_metric_statistics`` from several threads at
  once and gets throttled. botocore's adaptive mode backs off with jitter and
  keeps a client-side rate estimate, which is strictly better than a hand-
  rolled sleep loop.
* **A configurable endpoint.** Pointing ``endpoint_url`` at LocalStack is what
  makes the whole tool runnable without a cloud account; see
  ``docs/guide-local.md``.
* **A user agent.** Argus deployments are identifiable in CloudTrail, which
  matters when several tools drive the same account.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, TypeVar

from . import __version__

T = TypeVar("T")

#: Retries botocore performs before an error reaches Argus. Adaptive mode adds
#: client-side rate limiting on top of the exponential backoff, which is what
#: keeps a wide parallel wave from making throttling worse by retrying into it.
MAX_ATTEMPTS = 10
RETRY_MODE = "adaptive"

#: Errors that mean "try again later" rather than "this request is wrong".
#: botocore retries most of these itself; this set is for the polling loops,
#: which drive their own retries around a whole call sequence.
THROTTLE_CODES = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "ThrottledException",
        "RequestThrottled",
        "RequestThrottledException",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "SlowDown",
        "TransientError",
        "ServiceUnavailable",
        "InternalError",
        "InternalFailure",
        "RequestTimeout",
        "RequestTimeoutException",
    }
)


@dataclass(frozen=True)
class AwsSettings:
    """Everything that decides which account and endpoint Argus talks to."""

    region: str | None = None
    profile: str | None = None
    endpoint_url: str | None = None

    @property
    def is_local(self) -> bool:
        """True when pointed at an emulator rather than at AWS itself.

        Used to relax checks that only make sense against the real service,
        and to label output so a local run is never mistaken for a real one.
        """
        if not self.endpoint_url:
            return False
        return not self.endpoint_url.rstrip("/").endswith(".amazonaws.com")

    def describe(self) -> str:
        parts = []
        if self.profile:
            parts.append("profile=" + self.profile)
        parts.append("region=" + (self.region or os.environ.get("AWS_REGION") or "<default>"))
        if self.endpoint_url:
            parts.append("endpoint=" + self.endpoint_url)
        return ", ".join(parts)


def build_session(settings: AwsSettings):
    """A boto3 session honouring the profile and region Argus was given."""
    import boto3

    return boto3.session.Session(
        region_name=settings.region or None,
        profile_name=settings.profile or None,
    )


def build_client(session, service: str, settings: AwsSettings):
    """A boto3 client with Argus's retry policy and endpoint applied."""
    from botocore.config import Config

    config = Config(
        retries={"max_attempts": MAX_ATTEMPTS, "mode": RETRY_MODE},
        user_agent_extra="argus/" + __version__,
    )
    kwargs: dict[str, Any] = {"config": config}
    if settings.endpoint_url:
        kwargs["endpoint_url"] = settings.endpoint_url
    return session.client(service, **kwargs)


def error_code(exc: BaseException) -> str:
    """The AWS error code of a botocore exception, or an empty string."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if isinstance(code, str):
            return code
    return ""


def is_throttling(exc: BaseException) -> bool:
    return error_code(exc) in THROTTLE_CODES


def call_with_backoff(
    operation: Callable[[], T],
    *,
    attempts: int = 6,
    base_delay: float = 0.5,
    max_delay: float = 20.0,
    retry_on: Callable[[BaseException], bool] = is_throttling,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
) -> T:
    """Retry ``operation`` on throttling, with exponential backoff and jitter.

    botocore already retries a single API call. This wraps a *sequence* that
    must be retried as a unit -- notably "does the stack exist, then create or
    update it" -- where retrying only the inner call would leave the decision
    stale. Full jitter, not a fixed multiplier: several Argus threads throttled
    at the same instant must not wake up together and throttle again.
    """
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            if not retry_on(exc) or attempt == attempts - 1:
                raise
            last = exc
            delay = min(max_delay, base_delay * (2**attempt)) * jitter()
            sleep(delay)
    raise last if last else RuntimeError("call_with_backoff exhausted with no error")


def poll_delays(
    interval: float,
    timeout: float,
    *,
    jitter: Callable[[], float] = random.random,
) -> Iterable[float]:
    """Sleep intervals for a polling loop, jittered around ``interval``.

    Parallel waves poll many stacks; identical intervals make every thread call
    ``describe_stacks`` on the same tick. Spreading each sleep over
    [0.75, 1.25] x interval decorrelates them without changing how long the
    loop runs overall.
    """
    elapsed = 0.0
    while elapsed < timeout:
        delay = interval * (0.75 + 0.5 * jitter())
        remaining = timeout - elapsed
        delay = min(delay, remaining)
        yield delay
        elapsed += delay
