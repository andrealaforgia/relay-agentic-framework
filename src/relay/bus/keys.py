"""Every Redis key the framework uses, in one place."""


def ledger_key(swarm: str) -> str:
    return f"relay:{swarm}:ledger"


def seq_key(swarm: str) -> str:
    return f"relay:{swarm}:seq"


def dlq_key(swarm: str) -> str:
    return f"relay:{swarm}:dlq"


def done_key(swarm: str, role: str) -> str:
    return f"relay:{swarm}:done:{role}"


def timers_key(swarm: str) -> str:
    return f"relay:{swarm}:timers"


def presence_key(swarm: str, role: str, host: str) -> str:
    return f"relay:{swarm}:presence:{role}@{host}"


def snapshot_key(swarm: str) -> str:
    return f"relay:{swarm}:snapshot:coordinator"


def project_key(swarm: str) -> str:
    """Which project directory owns this swarm name (collision guard)."""
    return f"relay:{swarm}:project"


def group_name(role: str) -> str:
    return f"cg:{role}"
