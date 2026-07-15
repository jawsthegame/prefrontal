#!/bin/bash
#
# Guard against silent drift between the two app entitlements files:
#   Prefrontal.entitlements       — committed default, free-signing, NO aps-environment
#   Prefrontal.push.entitlements  — paid-tier opt-in, adds native APNs
#
# The push file must equal the base file PLUS exactly the `aps-environment` key —
# nothing else. So a new App Group / keychain group added to one but not the other
# fails here, loudly, instead of shipping a build that quietly drops an entitlement.
#
# Pure-Python (plistlib) so it runs anywhere — CI (.github/workflows/ios.yml) and
# by hand from the repo: `ios/scripts/check-entitlements-sync.sh`.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
BASE="$DIR/Prefrontal/Prefrontal.entitlements"
PUSH="$DIR/Prefrontal/Prefrontal.push.entitlements"

python3 - "$BASE" "$PUSH" <<'PY'
import plistlib
import pathlib
import sys

base_p, push_p = sys.argv[1], sys.argv[2]
base = plistlib.loads(pathlib.Path(base_p).read_bytes())
push = plistlib.loads(pathlib.Path(push_p).read_bytes())

errs = []
if "aps-environment" not in push:
    errs.append("push file is missing `aps-environment` (that's its whole reason to exist)")
if "aps-environment" in base:
    errs.append("base file unexpectedly declares `aps-environment` — free signing will break")

push_wo = {k: v for k, v in push.items() if k != "aps-environment"}
if push_wo != base:
    only_base = sorted(set(base) - set(push_wo))
    only_push = sorted(set(push_wo) - set(base))
    changed = sorted(k for k in set(base) & set(push_wo) if base[k] != push_wo[k])
    errs.append("the two files have drifted (they must match except for `aps-environment`):")
    if only_base:
        errs.append(f"    keys only in base: {only_base}")
    if only_push:
        errs.append(f"    keys only in push: {only_push}")
    if changed:
        errs.append(f"    keys with differing values: {changed}")

if errs:
    print("entitlements drift check FAILED:", file=sys.stderr)
    for e in errs:
        print("  " + e, file=sys.stderr)
    print(
        "\nKeep Prefrontal.push.entitlements = Prefrontal.entitlements + `aps-environment`.",
        file=sys.stderr,
    )
    sys.exit(1)

print("entitlements in sync: push = base + aps-environment ✓")
PY
