#!/bin/sh
# Independent Chromium fallback; tooling lives outside the app and outside /tmp.
set -eu

browser_qa_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
browser_qa_repo=$(dirname "$browser_qa_script_dir")
browser_qa_version=1.63.0
browser_qa_tools=${BROWSER_QA_TOOL_DIR:-${XDG_DATA_HOME:-"$HOME/.local/share"}/graphrag-browser-qa/playwright-$browser_qa_version}
export NODE_PATH="$browser_qa_tools/node_modules${NODE_PATH:+:$NODE_PATH}"

if ! node -e 'const p=process.argv[1]; if (require(p+"/package.json").version !== process.argv[2] || !require(p).chromium) process.exit(1)' "$browser_qa_tools/node_modules/playwright" "$browser_qa_version" 2>/dev/null; then
    npm install --prefix "$browser_qa_tools" --save-exact --no-audit --no-fund "playwright@$browser_qa_version"
fi

browser_qa_launch_check() {
    node -e 'const {chromium}=require(process.argv[1]); (async()=>{const b=await chromium.launch({headless:true,timeout:20000}); console.log("Chromium ready: "+b.version()); await b.close();})().catch(e=>{console.error(e.message);process.exitCode=1;})' "$browser_qa_tools/node_modules/playwright"
}
if ! browser_qa_launch_check; then
    # A module installation and a browser installation are separate. Retry
    # once with the official installer; never touch a personal Chrome profile.
    node "$browser_qa_tools/node_modules/playwright/cli.js" install chromium
    browser_qa_launch_check
fi

browser_qa_target=${1:---check}
if [ "$#" -gt 0 ]; then shift; fi
case "$browser_qa_target" in
    --check) browser_qa_target=verify_browser_connection.cjs ;;
    verify_*.cjs) ;;
    *) echo 'Usage: sh scripts/run_browser_qa.sh [--check|verify_<task>.cjs]' >&2; exit 2 ;;
esac
case "$browser_qa_target" in */*) echo 'Pass a script filename inside scripts/.' >&2; exit 2 ;; esac
test -f "$browser_qa_script_dir/$browser_qa_target"
cd "$browser_qa_repo"
# Refuse silently mixing the dedicated tool installation with app-local modules.
node -e 'const {createRequire}=require("node:module"); const {realpathSync}=require("node:fs"); const selected=createRequire(process.argv[1]).resolve("playwright"); if(realpathSync(selected)!==realpathSync(require.resolve(process.argv[2]))) throw Error("Playwright resolution conflict: "+selected)' "$browser_qa_script_dir/$browser_qa_target" "$browser_qa_tools/node_modules/playwright"
exec node "$browser_qa_script_dir/$browser_qa_target" "$@"
