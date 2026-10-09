import sys, os
_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _root)
os.environ["PYTHONPATH"] = _root
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PROMETHEUS_URL"] = "http://localhost:9090"
os.environ["EVO_WORKSPACE"] = "D:/05_code/credit-card-sys-ops/.evoontology"
os.environ["RCA_FRONTEND_ORIGIN"] = "*"
from backend.main import app
import uvicorn
uvicorn.run(app, host="0.0.0.0", port=8088, log_level="info")
