# Notification rate limiter

A small Python prototype I built as part of my attempts to practice LLD.
LLD temp project 2 till now after RobustWorkerQueue.
It is one module, `rate_limiter.py`, with no dependencies beyond the standard library. The tests use pytest.
## What it does

Each level has a cap per sliding 10-minute window. The prototype uses 3 per user, 10 per team and 20 per company in tests, and the caps are plain config. A send goes out only if it fits under all three levels.

```python
decision = limiter.allow(request_id="r1", user_id="alice", channel=Channel.EMAIL)
# Decision(outcome=ALLOWED | REJECTED | ERROR, rejection_reason, retry_after, fallback, error_reason)
```

- A rejected send comes back with `retry_after`, the seconds until it would fit under every full level.
- Sending the same `request_id` again returns the original answer and is not counted twice, so a caller can safely retry after a timeout.
- An unknown user is an `ERROR`, not a rejection.
- If the log store is down, email is let through and SMS is blocked. Both answers carry `fallback=True`.

## How it is built

| Class | Job |
|---|---|
| `RateLimiter` | Runs one request: look up the user's team and company, ask the store, build the `Decision`. |
| `LogStore` | Keeps a timestamped log per key (`user:alice`, `team:design`). Checks every level and writes to all of them in one step, under a lock. |
| `MappingCache` | Remembers each user's team and company for a fixed time, then asks the org service again. |
| `OrgLookup` | A `Protocol` for the org service. The prototype only defines the interface. |
| `FallbackPolicy` | A `Protocol` with two implementations, `FailOpen` and `FailClosed`. Config picks one per channel. |

## Design choices worth reading

- **Sliding window log.** The store keeps every accepted timestamp, so the count is exact and `retry_after` is an exact number. An entry stops counting at `ts + 600`, not one tick later.
- **The store owns check and write.** Two threads can never both see room for the last slot. A rejected request writes nothing to any level, so a full team does not use up a user's cap.
- **The clock is injected.** Tests use a `FakeClock` and move time by hand, so no test sleeps.
- **One rejection reason.** `RATE_LIMITED` covers all three levels, because a caller does the same thing whichever level is full. The store still reports which levels were full, for logs.
- **Strategy only where it pays.** The fallback policy is the one place with two swappable behaviors. `RateLimiter` is already the context, so there is no extra wrapper class, and the policy never sees the channel.
- **Narrow failure handling.** `allow()` catches `StoreUnavailable` around the store call and nothing else. A bug in the limiter should fail loudly, not turn into "email allowed, SMS blocked".
- **Unknown users are not cached.** A newly created user is found on the next call. The cost is one org lookup per call for a user that does not exist.

## Limits

This is a single-process prototype. The lock protects threads in one process, and the log lives in memory. A real deployment would put the same `try_admit` method behind a shared store, such as Redis, and keep the three keys on one node so the check-and-write stays atomic.

## Run it

Python 3.11 or newer (the code uses `StrEnum`).

```
python3 demo.py                 # a printed walkthrough of the main cases
python3 -m pip install pytest
python3 -m pytest -q            # 16 tests
python3 visualize.py            # writes trace.json and visual.html
open visual.html                # an animated view of the sliding window
```

The tests go through the public calls (`allow` and `try_admit`) and use fakes with real behavior, not mocks. One test starts 16 threads at the same instant to check the cap holds. It fails when the lock is removed.

## Files

```
rate_limiter.py       the limiter
test_rate_limiter.py  behavior tests
conftest.py           FakeClock, FakeOrgLookup, a store that is down, fixtures
demo.py               printed walkthrough, and the place where the objects are wired together
visualize.py          runs scenarios through the real limiter and writes the animated page
LLD_Entities          the class design notes
```
