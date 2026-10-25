import pytest

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


class FakeClock:
    """Manual clock for tests. Time moves only when a test calls advance()."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeOrgLookup:
    """Stands in for the org service. A test sets who is in which team, and can
    change it later to simulate a transfer. Satisfies the OrgLookup protocol."""

    def __init__(self):
        self._members: dict[str, Membership] = {}

    def set(self, user_id: str, team_id: str, company_id: str) -> None:
        self._members[user_id] = Membership(team_id=team_id, company_id=company_id)

    def lookup(self, user_id: str) -> Membership | None:
        return self._members.get(user_id)


class UnavailableStore:
    """A log store that is down. Every call fails the way a lost connection would."""

    def try_admit(self, scopes, request_id):
        raise StoreUnavailable()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def org() -> FakeOrgLookup:
    return FakeOrgLookup()


@pytest.fixture
def cache(org: FakeOrgLookup, clock: FakeClock) -> MappingCache:
    return MappingCache(org_lookup=org, ttl_seconds=300, clock=clock)


@pytest.fixture
def store(clock: FakeClock) -> LogStore:
    return LogStore(limits=Limits(user_cap=3, team_cap=10, company_cap=20), clock=clock)


@pytest.fixture
def down_store() -> UnavailableStore:
    return UnavailableStore()


@pytest.fixture
def make_limiter(cache: MappingCache, clock: FakeClock):
    """Builds a RateLimiter with real cache and store. A test picks the caps, and
    can swap in a broken store. Email fails open and SMS fails closed, as in the Spec."""

    def build(limits: Limits = Limits(user_cap=3, team_cap=10, company_cap=20), log_store=None):
        return RateLimiter(
            mapping_cache=cache,
            log_store=log_store or LogStore(limits=limits, clock=clock),
            limits=limits,
            policies={Channel.EMAIL: FailOpen(), Channel.SMS: FailClosed()},
        )

    return build
