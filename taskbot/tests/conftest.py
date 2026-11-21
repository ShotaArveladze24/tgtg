from datetime import datetime, timezone

import pytest

from taskbot.config import Config
from taskbot.db import Database
from taskbot.services import TaskService

# Friday 20/11/2026 10:00 Europe/Rome (09:00 UTC)
FIXED_NOW = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)


class FrozenService(TaskService):
    current = FIXED_NOW

    def now(self):
        return self.current


@pytest.fixture
def cfg():
    return Config(token="x", allowed_users={1})


@pytest.fixture
def svc(cfg):
    s = FrozenService(Database(":memory:"), cfg)
    s.current = FIXED_NOW
    return s
