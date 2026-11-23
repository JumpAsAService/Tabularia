"""Browser check of the editor of Bayesian models' keyboard, on Chromium and Firefox of Playwright, with a
PC's keyboard and a Mac's (docs/specs/bayes-graph-editor.md, «Keyboard»). It runs browser_check.cjs once
per browser against `nuxt dev` (a made-up `tab_token` cookie, no backend).

    UI=http://127.0.0.1:3123 PLAYWRIGHT_CORE=<folder of playwright-core> python3 e2e/bayes_editor/browser_check.py [chromium firefox]

Exits 1 if any check is KO.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
browsers = sys.argv[1:] or ["chromium", "firefox"]
failed = 0
for b in browsers:
    failed += subprocess.run(["node", os.path.join(HERE, "browser_check.cjs"), b]).returncode != 0
sys.exit(1 if failed else 0)
