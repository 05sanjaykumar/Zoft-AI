"""The agent an inbound call gets before anyone has configured one."""

import json

from app.config import ROOT
from app.schemas import CallConfig

DEFAULT_CONFIG_PATH = ROOT / "examples" / "inbound-clinic.json"


def default_inbound_config() -> CallConfig:
    data = json.loads(DEFAULT_CONFIG_PATH.read_text())
    data["direction"] = "inbound"
    return CallConfig.model_validate(data)
