import sys, os
_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _root)
os.environ["PYTHONPATH"] = _root
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PROMETHEUS_URL"] = "http://localhost:9090"
# <repo> = rca-agent 的上一级；不写死宿主绝对路径
os.environ["EVO_WORKSPACE"] = os.path.join(os.path.dirname(_root), ".evoontology")
os.environ["RCA_FRONTEND_ORIGIN"] = "*"
from backend.main import app
import uvicorn
uvicorn.run(app, host="0.0.0.0", port=8088, log_level="info")
