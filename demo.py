"""Walkthrough of the rate limiter. Run with: python3 demo.py

main() is the composition root: the one place that builds the real objects and
wires them together. Nothing in rate_limiter.py creates its own collaborators.
"""

from rate_limiter import (
    Channel,
    FailClosed,
    FailOpen,
    Limits,
    LogStore,
    MappingCache,
    Membership,
    RateLimiter,
    StoreUnavailable,
)


class Clock:
    """A clock we can move by hand, so the demo can skip ahead 10 minutes."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class StaticOrgLookup:
    """Stands in for the org service: a fixed table of who belongs where."""

    def __init__(self, members: dict[str, Membership]):
        self._members = members

    def lookup(self, user_id: str) -> Membership | None:
        return self._members.get(user_id)


class BrokenStore:
    """A log store that is down, to show the fallback policies."""

    def try_admit(self, scopes, request_id):
        raise StoreUnavailable()


def show(label: str, decision) -> None:
    detail = decision.rejection_reason or decision.error_reason or ""
    extra = f" retry_after={decision.retry_after:g}s" if decision.retry_after is not None else ""
    fallback = " (fallback)" if decision.fallback else ""
    print(f"  {label:<34} {decision.outcome}{' ' + detail if detail else ''}{extra}{fallback}")


def main() -> None:
    clock = Clock()
    limits = Limits(user_cap=3, team_cap=5, company_cap=8)
    org = StaticOrgLookup({
        "alice": Membership(team_id="design", company_id="acme"),
        "bob": Membership(team_id="design", company_id="acme"),
        "carol": Membership(team_id="design", company_id="acme"),
    })
    policies = {Channel.EMAIL: FailOpen(), Channel.SMS: FailClosed()}
    limiter = RateLimiter(
        mapping_cache=MappingCache(org_lookup=org, ttl_seconds=300, clock=clock),
        log_store=LogStore(limits=limits, clock=clock),
        limits=limits,
        policies=policies,
    )

    print("1. A user has 3 sends per 10 minutes")
    for i in range(1, 5):
        show(f"alice sends #{i}", limiter.allow(f"a{i}", "alice", Channel.EMAIL))

    print("\n2. A retry of an allowed send is not counted twice")
    show("alice retries a1 (same id)", limiter.allow("a1", "alice", Channel.EMAIL))

    print("\n3. The team cap (5) blocks someone who sent nothing")
    show("bob sends #1", limiter.allow("b1", "bob", Channel.EMAIL))
    show("bob sends #2", limiter.allow("b2", "bob", Channel.EMAIL))
    show("carol sends (team is full)", limiter.allow("c1", "carol", Channel.EMAIL))

    print("\n4. An unknown user is an error, not a rejection")
    show("dave sends", limiter.allow("d1", "dave", Channel.EMAIL))

    print("\n5. After 10 minutes the window slides and sends count again")
    clock.now = 600
    show("alice sends again", limiter.allow("a5", "alice", Channel.EMAIL))

    print("\n6. Store down: email fails open, SMS fails closed")
    broken = RateLimiter(
        mapping_cache=MappingCache(org_lookup=org, ttl_seconds=300, clock=clock),
        log_store=BrokenStore(),
        limits=limits,
        policies=policies,
    )
    show("alice sends email", broken.allow("e1", "alice", Channel.EMAIL))
    show("alice sends sms", broken.allow("s1", "alice", Channel.SMS))


if __name__ == "__main__":
    main()
