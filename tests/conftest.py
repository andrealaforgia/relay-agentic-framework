import fakeredis
import pytest

from relay.bus.publisher import Publisher
from relay.contract import ContractValidator, load_contract

CONTRACT = load_contract()


@pytest.fixture
def client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def validator() -> ContractValidator:
    return ContractValidator(CONTRACT)


@pytest.fixture
def publisher(client: fakeredis.FakeRedis, validator: ContractValidator) -> Publisher:
    return Publisher(client, validator, "testswarm")
