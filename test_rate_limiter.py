import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from rate_limiter import (
    AdmitStatus,
    Channel,
    ErrorReason,
    Limits,
    Membership,
    Outcome,
    RejectionReason,
    ScopeLimit,
)


def user_scope(cap: int = 3) -> list[ScopeLimit]:
    """The one level these store tests check: user u1 with the given cap."""
    return [ScopeLimit(scope="user", id="u1", limit=cap)]


def user_and_team(user_id: str, user_cap: int, team_cap: int) -> list[ScopeLimit]:
    """Two levels, user first, as try_admit expects. Everyone is in team t1."""
    return [
        ScopeLimit(scope="user", id=user_id, limit=user_cap),
        ScopeLimit(scope="team", id="t1", limit=team_cap),
    ]


# --- LogStore.try_admit ---
def test_first_request_within_cap_is_accepted(store):
    result = store.try_admit(user_scope(), "r1")        # act

    assert result.status == AdmitStatus.ACCEPTED         # assert


def test_request_over_the_cap_is_rejected_with_retry_after(store, clock):
    store.try_admit(user_scope(), "r1")                  # t=0
    clock.advance(60)
    store.try_admit(user_scope(), "r2")                  # t=60
    clock.advance(60)
    store.try_admit(user_scope(), "r3")                  # t=120, cap of 3 is now full
    clock.advance(80)                                    # t=200

    result = store.try_admit(user_scope(), "r4")         # act

    assert result.status == AdmitStatus.REJECTED
    assert result.blocked_scopes == ("user",)
    assert result.retry_after == 400                     # r1 expires at 0 + 600, now is 200


def test_entry_stops_counting_exactly_at_window_end(store, clock):
    store.try_admit(user_scope(), "r1")                  # t=0
    clock.advance(60)
    store.try_admit(user_scope(), "r2")                  # t=60
    clock.advance(60)
    store.try_admit(user_scope(), "r3")                  # t=120, full
    clock.advance(479)                                   # t=599, r1 still counts

    just_before = store.try_admit(user_scope(), "r4")
    clock.advance(1)                                     # t=600, r1 expires exactly now
    at_boundary = store.try_admit(user_scope(), "r5")

    assert just_before.status == AdmitStatus.REJECTED
    assert at_boundary.status == AdmitStatus.ACCEPTED


def test_duplicate_request_id_is_seen_and_counted_once(store):
    first = store.try_admit(user_scope(cap=2), "r1")
    repeat = store.try_admit(user_scope(cap=2), "r1")    # a retry of the same send
    second = store.try_admit(user_scope(cap=2), "r2")    # fits only if r1 counted once
    third = store.try_admit(user_scope(cap=2), "r3")     # now the cap of 2 is full

    assert first.status == AdmitStatus.ACCEPTED
    assert repeat.status == AdmitStatus.SEEN
    assert second.status == AdmitStatus.ACCEPTED
    assert third.status == AdmitStatus.REJECTED


def test_retry_at_full_cap_is_seen_not_rejected(store):
    store.try_admit(user_scope(cap=1), "r1")             # cap of 1 is now full

    retry = store.try_admit(user_scope(cap=1), "r1")     # the caller retried after a timeout

    assert retry.status == AdmitStatus.SEEN              # the duplicate check runs before the limit check


def test_rejected_request_writes_nothing_at_any_level(store):
    store.try_admit(user_and_team("u1", user_cap=3, team_cap=1), "r1")   # team of 1 is now full

    blocked = store.try_admit(user_and_team("u2", user_cap=3, team_cap=1), "r2")
    # Same user u2, but the team has room this time. If the rejected r2 had left an
    # entry under u2, this user cap of 1 would already be full.
    later = store.try_admit(user_and_team("u2", user_cap=1, team_cap=10), "r3")

    assert blocked.status == AdmitStatus.REJECTED
    assert blocked.blocked_scopes == ("team",)
    assert later.status == AdmitStatus.ACCEPTED


def test_several_full_levels_are_all_reported_and_the_longest_wait_wins(store, clock):
    store.try_admit(user_and_team("u2", user_cap=5, team_cap=2), "r1")   # t=0, team: [0]
    clock.advance(100)
    store.try_admit(user_and_team("u1", user_cap=1, team_cap=2), "r2")   # t=100, user u1: [100], team: [0, 100]
    clock.advance(100)                                                   # t=200

    result = store.try_admit(user_and_team("u1", user_cap=1, team_cap=2), "r3")

    assert result.status == AdmitStatus.REJECTED
    assert result.blocked_scopes == ("user", "team")
    # The team frees a slot at 0 + 600 (wait 400). The user frees at 100 + 600 (wait 500).
    # The send needs both, so the longer wait is the answer.
    assert result.retry_after == 500


def test_parallel_requests_never_exceed_the_cap(store):
    cap, threads, trials = 5, 16, 50
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)                          # switch threads often so a missing lock shows up
    try:
        for trial in range(trials):
            scope = [ScopeLimit(scope="user", id=f"u{trial}", limit=cap)]   # fresh key each trial
            start_together = threading.Barrier(threads)

            def send(i):
                start_together.wait()                    # all threads hit try_admit at the same instant
                return store.try_admit(scope, f"r{i}")

            with ThreadPoolExecutor(max_workers=threads) as pool:
                results = list(pool.map(send, range(threads)))

            accepted = [r for r in results if r.status == AdmitStatus.ACCEPTED]
            assert len(accepted) == cap, f"trial {trial}: {len(accepted)} accepted"
    finally:
        sys.setswitchinterval(old_interval)


# --- MappingCache.get_mapping ---
def test_cached_mapping_is_served_until_the_ttl_then_refreshed(cache, org, clock):
    org.set("u1", team_id="teamA", company_id="c1")
    assert cache.get_mapping("u1") == Membership("teamA", "c1")      # t=0, fetched and cached

    org.set("u1", team_id="teamB", company_id="c1")                  # u1 transfers teams
    clock.advance(299)
    still_cached = cache.get_mapping("u1")                           # t=299, inside the 300 s TTL
    clock.advance(1)
    refreshed = cache.get_mapping("u1")                              # t=300, TTL is up

    assert still_cached == Membership("teamA", "c1")
    assert refreshed == Membership("teamB", "c1")


def test_unknown_user_is_none_and_found_once_they_exist(cache, org):
    before = cache.get_mapping("u9")                                 # org service has never heard of u9
    org.set("u9", team_id="teamA", company_id="c1")                  # u9 is created
    after = cache.get_mapping("u9")                                  # no waiting for a TTL

    assert before is None
    assert after == Membership("teamA", "c1")


# --- RateLimiter.allow ---
def test_send_within_all_caps_is_allowed(make_limiter, org):
    org.set("u1", team_id="teamA", company_id="c1")
    limiter = make_limiter()

    decision = limiter.allow("r1", "u1", Channel.EMAIL)

    assert decision.outcome == Outcome.ALLOWED
    assert decision.retry_after is None                  # a wait only makes sense on a rejection
    assert decision.fallback is False


def test_user_over_their_cap_is_rejected_with_the_wait(make_limiter, org, clock):
    org.set("u1", team_id="teamA", company_id="c1")
    limiter = make_limiter()                             # user cap 3
    limiter.allow("r1", "u1", Channel.EMAIL)             # t=0
    clock.advance(60)
    limiter.allow("r2", "u1", Channel.EMAIL)             # t=60
    clock.advance(60)
    limiter.allow("r3", "u1", Channel.EMAIL)             # t=120, user is full
    clock.advance(80)                                    # t=200

    decision = limiter.allow("r4", "u1", Channel.EMAIL)

    assert decision.outcome == Outcome.REJECTED
    assert decision.rejection_reason == RejectionReason.RATE_LIMITED
    assert decision.retry_after == 400                   # r1 frees a slot at 600, now is 200


def test_team_cap_blocks_a_user_who_has_sent_nothing(make_limiter, org):
    org.set("u1", team_id="teamA", company_id="c1")
    org.set("u2", team_id="teamA", company_id="c1")
    org.set("u3", team_id="teamA", company_id="c1")
    org.set("u4", team_id="teamB", company_id="c1")
    limiter = make_limiter(Limits(user_cap=5, team_cap=2, company_cap=20))
    limiter.allow("r1", "u1", Channel.EMAIL)
    limiter.allow("r2", "u2", Channel.EMAIL)             # teamA is now full

    teammate = limiter.allow("r3", "u3", Channel.EMAIL)  # u3's own count is 0
    other_team = limiter.allow("r4", "u4", Channel.EMAIL)

    assert teammate.outcome == Outcome.REJECTED
    assert other_team.outcome == Outcome.ALLOWED


def test_unknown_user_is_an_error_not_a_rejection(make_limiter):
    limiter = make_limiter()

    decision = limiter.allow("r1", "nobody", Channel.EMAIL)

    assert decision.outcome == Outcome.ERROR
    assert decision.error_reason == ErrorReason.UNKNOWN_USER


def test_retry_of_an_allowed_send_is_allowed_and_uses_no_extra_cap(make_limiter, org):
    org.set("u1", team_id="teamA", company_id="c1")
    limiter = make_limiter(Limits(user_cap=1, team_cap=10, company_cap=20))
    first = limiter.allow("r1", "u1", Channel.EMAIL)

    retry = limiter.allow("r1", "u1", Channel.EMAIL)     # caller timed out and sent the same id
    new_send = limiter.allow("r2", "u1", Channel.EMAIL)

    assert first.outcome == Outcome.ALLOWED
    assert retry.outcome == Outcome.ALLOWED
    assert new_send.outcome == Outcome.REJECTED          # the retry did not count a second time


def test_store_down_lets_email_through_and_blocks_sms(make_limiter, org, down_store):
    org.set("u1", team_id="teamA", company_id="c1")
    limiter = make_limiter(log_store=down_store)

    email = limiter.allow("r1", "u1", Channel.EMAIL)
    sms = limiter.allow("r2", "u1", Channel.SMS)

    assert email.outcome == Outcome.ALLOWED
    assert email.fallback is True
    assert sms.outcome == Outcome.REJECTED
    assert sms.rejection_reason == RejectionReason.STORE_UNAVAILABLE
    assert sms.fallback is True
