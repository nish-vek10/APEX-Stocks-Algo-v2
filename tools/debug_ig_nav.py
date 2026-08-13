# tools/debug_ig_nav.py
"""Quick diagnostic — check what IG navigation API actually returns."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from core.utils.config_loader import load_production_config, resolve_ig_credentials
from prod.execution.ig_connector import IGConnector

prod_cfg = load_production_config(ROOT)
creds    = resolve_ig_credentials(prod_cfg)

connector = IGConnector(creds)
with connector:
    svc = connector.service
    base = svc.BASE_URL
    print(f"Base URL : {base}")
    print(f"Session headers: {dict(svc.session.headers)}\n")

    # Try navigation with different versions
    for ver in ["1", "2"]:
        url = f"{base}/marketnavigation"
        resp = svc.session.get(url, headers={"Version": ver}, timeout=15)
        print(f"GET /marketnavigation  version={ver}")
        print(f"  Status : {resp.status_code}")
        print(f"  Body   : {resp.text[:500] if resp.text else '(empty)'}")
        print()

    # Also try search_markets to confirm session works
    print("Testing search_markets('Apple')...")
    r = svc.search_markets(search_term="Apple")
    print(f"  Result type: {type(r)}")
    if hasattr(r, 'head'):
        print(r.head(3))
