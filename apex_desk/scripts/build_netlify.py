# apex_desk/scripts/build_netlify.py
"""Build the Netlify bundle: python scripts/build_netlify.py <SUPABASE_URL> <SUPABASE_ANON_KEY>  -> apex_desk/netlify_site/ (drag into Netlify)."""
import json
import shutil
import sys
from pathlib import Path

if len(sys.argv) != 3 or "service" in sys.argv[2].lower():
    sys.exit("usage: build_netlify.py <SUPABASE_URL> <ANON_KEY>   (use the ANON/publishable key, NEVER the service_role key)")
root = Path(__file__).resolve().parents[1]
out = root / "netlify_site"
out.mkdir(exist_ok=True)
for f in ["style.css", "app.js", "cloud.js"]:
    shutil.copy(root / "web" / f, out / f)
(out / "config.js").write_text("window.DESK_CONFIG = " + json.dumps({"SUPABASE_URL": sys.argv[1], "SUPABASE_ANON_KEY": sys.argv[2]}) + ";\n", encoding="utf-8")
(out / "index.html").write_text((root / "web" / "index.html").read_text(encoding="utf-8").replace('"/static/', '"./'), encoding="utf-8")
(out / "_headers").write_text("/*\n  X-Robots-Tag: noindex\n  Cache-Control: no-cache\n", encoding="utf-8")
print("Built", out, "-> drag this folder onto Netlify")
