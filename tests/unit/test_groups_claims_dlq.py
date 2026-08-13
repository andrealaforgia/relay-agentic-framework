from relay.bus import claims, dedup, dlq, groups
from relay.bus.keys import dlq_key, group_name, ledger_key

STREAM = ledger_key("testswarm")
GROUP = group_name("coordinator")


def _publish_problem(publisher, n=1):
    return [
        publisher.send("owner", "interpreter", "chat.problem", {"text": f"p{i}"})
        for i in range(n)
    ]


def test_ensure_group_is_idempotent(client) -> None:
    groups.ensure_group(client, STREAM, GROUP)
    groups.ensure_group(client, STREAM, GROUP)  # BUSYGROUP swallowed


def test_group_created_at_zero_sees_prior_history(client, publisher) -> None:
    _publish_problem(publisher, 2)
    groups.ensure_group(client, STREAM, GROUP)  # created AFTER the messages
    deliveries = groups.read_new(client, STREAM, GROUP, "c1", block_ms=1)
    assert [d.envelope.seq for d in deliveries] == [1, 2]


def test_pending_redelivered_until_acked(client, publisher) -> None:
    groups.ensure_group(client, STREAM, GROUP)
    _publish_problem(publisher)
    (d,) = groups.read_new(client, STREAM, GROUP, "c1", block_ms=1)
    # crash before ack: a fresh PEL drain re-delivers it
    (pending,) = groups.read_pending(client, STREAM, GROUP, "c1")
    assert pending.stream_id == d.stream_id
    groups.ack(client, STREAM, GROUP, d.stream_id)
    assert groups.read_pending(client, STREAM, GROUP, "c1") == []


def test_autoclaim_steals_from_dead_consumer(client, publisher) -> None:
    groups.ensure_group(client, STREAM, GROUP)
    _publish_problem(publisher)
    groups.read_new(client, STREAM, GROUP, "dead-consumer", block_ms=1)  # never acks
    stolen = claims.autoclaim_stale(client, STREAM, GROUP, "standby", min_idle_ms=0)
    assert len(stolen) == 1
    assert stolen[0].envelope.type == "chat.problem"


def test_delivery_count_increases_on_reclaim(client, publisher) -> None:
    groups.ensure_group(client, STREAM, GROUP)
    _publish_problem(publisher)
    (d,) = groups.read_new(client, STREAM, GROUP, "c1", block_ms=1)
    assert claims.delivery_count(client, STREAM, GROUP, d.stream_id) == 1
    claims.autoclaim_stale(client, STREAM, GROUP, "c2", min_idle_ms=0)
    assert claims.delivery_count(client, STREAM, GROUP, d.stream_id) == 2


def test_done_map_dedup(client) -> None:
    assert dedup.already_done(client, "testswarm", "builder", "01J5AB3CDEF4GH5JK6MN7PQ8RS") is None
    dedup.mark_done(client, "testswarm", "builder", "01J5AB3CDEF4GH5JK6MN7PQ8RS", "01J5AB3CDEF4GH5JK6MN7PQ8RT")
    assert (
        dedup.already_done(client, "testswarm", "builder", "01J5AB3CDEF4GH5JK6MN7PQ8RS")
        == "01J5AB3CDEF4GH5JK6MN7PQ8RT"
    )


def test_dlq_routing_writes_dlq_and_ledger_event(client, publisher) -> None:
    raw = {"event_id": "01J5AB3CDEF4GH5JK6MN7PQ8RS", "type": "work.done", "from": "builder"}
    dlq.route_to_dlq(client, publisher, "testswarm", "coordinator", "off_contract", raw, "bad vocab")
    assert dlq.dlq_depth(client, "testswarm") == 1
    ((_id, fields),) = client.xrange(dlq_key("testswarm"))
    assert fields["reason"] == "off_contract"
    assert fields["original_event_id"] == "01J5AB3CDEF4GH5JK6MN7PQ8RS"
    # the audit trail records the routing as a first-class event
    events = client.xrange(STREAM)
    assert any(f["type"] == "system.dlq_routed" for _i, f in events)
