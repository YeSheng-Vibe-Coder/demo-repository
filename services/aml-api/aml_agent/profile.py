"""One versioned configuration source shared by runtime modules."""

import hashlib
import json

from .settings import ROOT

_bytes = (ROOT / "docs/architecture/AML_Agent_Config_v1.0.json").read_bytes()
PROFILE = json.loads(_bytes)
PROFILE_SHA = hashlib.sha256(_bytes).hexdigest()
PLANNER = PROFILE["planner"]
TOOLS = PROFILE["tools"]
PROVIDER = PROFILE["provider"]
EVIDENCE = PROFILE["evidence_packet"]
MARKOVIAN = PROFILE["markovian"]
