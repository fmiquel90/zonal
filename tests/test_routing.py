import pytest

from zonal.model import Host, NoHealthyHostError
from zonal.routing import Router


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_empty_cache_raises():
    with pytest.raises(NoHealthyHostError):
        Router().pick()


def test_round_robin_cycles():
    r = Router()
    a, b = Host("10.0.0.1", 80), Host("10.0.0.2", 80)
    r.update([a, b])
    picks = [r.pick() for _ in range(4)]
    assert picks == [a, b, a, b]


def test_breaker_skips_then_recovers():
    clock = FakeClock()
    r = Router(breaker_cooldown=10.0, clock=clock)
    a, b = Host("10.0.0.1", 80), Host("10.0.0.2", 80)
    r.update([a, b])
    r.mark_down(a)
    # a is in cooldown -> only b is handed out
    assert {r.pick() for _ in range(4)} == {b}
    clock.t = 11.0
    assert a in {r.pick() for _ in range(4)}


def test_all_dead_still_returns():
    r = Router()
    a = Host("10.0.0.1", 80)
    r.update([a])
    r.mark_down(a)
    assert r.pick() == a  # degraded beats refusing all traffic


def test_update_prunes_stale_breaker_entries():
    clock = FakeClock()
    r = Router(clock=clock)
    a, b = Host("10.0.0.1", 80), Host("10.0.0.2", 80)
    r.update([a, b])
    r.mark_down(a)
    r.update([b])  # a gone
    r.update([a, b])  # a back, breaker should have been pruned
    assert a in {r.pick() for _ in range(4)}
