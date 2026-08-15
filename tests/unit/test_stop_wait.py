"""The Stop hook holds the door when the swarm owes the Interpreter a reply.

Measured failure (sandtris, 15 Aug): the interpreter dispatched
analysis.requested at 20:59:39, its turn ended at 20:59:55, the analyst
answered at 21:02:51 — and the questions sat unread for the rest of the
evening, because an idle Claude Code session checks its mail only when the
Owner types. Waiting in the hook costs nothing: relay-inbox blocks on Redis.
"""

from relay.cli.inbox import _awaiting_reply


def test_nothing_outstanding_after_a_plain_exchange(client, publisher) -> None:
    publisher.send("owner", "interpreter", "problem.stated", {"text": "a sand game"})
    publisher.send("interpreter", "owner", "update.shared", {"text": "Understood."})
    assert _awaiting_reply(client, "testswarm") is False


def test_outstanding_once_work_is_dispatched_downstream(client, publisher) -> None:
    publisher.send("interpreter", "analyst", "analysis.requested", {"problem": "a sand game"})
    assert _awaiting_reply(client, "testswarm") is True


def test_settled_again_once_the_answer_lands(client, publisher) -> None:
    publisher.send("interpreter", "analyst", "analysis.requested", {"problem": "a sand game"})
    publisher.send("analyst", "interpreter", "questions.raised",
                   {"question_id": "q-01J5AB3CDEF4GH5JK6MN7PQ8RS", "questions": ["Which physics?"]})
    assert _awaiting_reply(client, "testswarm") is False


def test_a_second_round_is_outstanding_again(client, publisher) -> None:
    publisher.send("interpreter", "analyst", "analysis.requested", {"problem": "a sand game"})
    publisher.send("analyst", "interpreter", "questions.raised",
                   {"question_id": "q-01J5AB3CDEF4GH5JK6MN7PQ8RS", "questions": ["Which physics?"]})
    publisher.send("interpreter", "analyst", "answers.relayed",
                   {"question_id": "q-01J5AB3CDEF4GH5JK6MN7PQ8RS", "answers": ["Falling sand"]})
    assert _awaiting_reply(client, "testswarm") is True
