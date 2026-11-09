import sys, os
sys.path.insert(0, os.getcwd())
from qidi_mcp.config import load_settings
from qidi_mcp.policy import *
from qidi_mcp.errors import *
def mk(**env):
    base = {"QIDI_PRINTER_URL": "http://127.0.0.1:7125", "QIDI_TEST_MODE": "1"}
    base.update(env)
    return SafetyPolicy(load_settings(base))
