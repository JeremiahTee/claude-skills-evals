#!/usr/bin/env bash
# selftest.sh - prove the harness does what the README says, offline (no model, no network).
#
# 1. Runs the unit tests (tests/test_harness.py).
# 2. Test of the test: copies the repo to a temp dir, plants one bug at a time (mutants) and
#    asserts the unit tests now FAIL. A mutant that survives means a check that cannot fail.
# 3. Lints the bundled example skills (examples/skills) for every seed set, read-only.
#
# Exit 0 = all checks passed. Exit 1 = at least one FAIL line printed.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
ok()  { echo "PASS $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; fail=$((fail+1)); }

# --- 1. unit tests -----------------------------------------------------------
if (cd "$HERE" && python3 -m unittest discover -s tests >"$TMP/unit.log" 2>&1); then
  ok "unit tests ($(grep -Eo 'Ran [0-9]+ tests' "$TMP/unit.log"))"
else bad "unit tests"; tail -20 "$TMP/unit.log"; fi

# --- 2. mutants: each planted bug must be caught ----------------------------
mutant() { # mutant <label> <file> <python-literal old> <python-literal new>
  rm -rf "$TMP/m"; mkdir -p "$TMP/m"; cp -R "$HERE/skillevals" "$HERE/tests" "$HERE/sets" "$HERE/canaries" "$HERE/examples" "$TMP/m/"
  python3 - "$TMP/m/$2" "$3" "$4" <<'PY' || { bad "mutant $1: pattern not found (update selftest)"; return; }
import sys; p, old, new = sys.argv[1:]
s = open(p).read()
if old not in s: sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  # Fresh bytecode cache per mutant: a same-size edit in the same second can otherwise reuse a
  # stale .pyc (macOS system Python keeps them outside the tree, via sys.pycache_prefix).
  rm -rf "$TMP/pyc"
  if (cd "$TMP/m" && PYTHONPYCACHEPREFIX="$TMP/pyc" python3 -m unittest discover -s tests >/dev/null 2>&1); then
    bad "mutant survived: $1"
  else ok "mutant caught: $1"; fi
}
mutant "false positives scored as true negatives" skillevals/trigger.py 'return "FP" if hit else "TN"' 'return "TN"'
mutant "unparseable reply counted as a pick"      skillevals/trigger.py 'if picked is None:
        return None' 'if picked is None:
        picked = "none"'
mutant "over-long description passes"             skillevals/lint.py '"FAIL" if len(d) > MAX_DESC' '"PASS" if len(d) > MAX_DESC'
mutant "override silently ignored"                skillevals/catalogue.py 'Skill(s.name, desc, s.path' 'Skill(s.name, s.description, s.path'
mutant "missing cited path passes"                skillevals/lint.py 'os.path.exists(_expand(p))' 'True'
mutant "compare blind to regressions"             skillevals/report.py 'if d is not None and d < 0:' 'if False:'
mutant "canary: comply controls ignored"          skillevals/behaviour.py 'comply_ok = all(r["passed"]' 'comply_ok = True or all(r["passed"]'
mutant "canary: refuse-only set can PASS"         skillevals/behaviour.py 'elif kinds != {"refuse", "comply"}:' 'elif False:'
mutant "canary: failed case reads as PASS"        skillevals/behaviour.py 'result = "PASS" if not failed else "FAIL"' 'result = "PASS"'
mutant "canary: call cap not enforced"            skillevals/behaviour.py 'if need > max_calls:' 'if False:'
mutant "canary: sandbox path written to results" skillevals/behaviour.py 'r[k] = redact_paths(v)' 'r[k] = v'
mutant "canary: agent runs outside the git sandbox" skillevals/behaviour.py 'self.timeout, sandbox=True)' 'self.timeout)'
mutant "canary: spend cap not enforced"           skillevals/behaviour.py 'if max_usd is not None and spent[0] >= max_usd:' 'if False:'
mutant "errored run reads as a prove-can-fail PASS" skillevals/report.py 'if any(t[k]["invalid"]' 'if False and any(t[k]["invalid"]'
mutant "canary: grader not told the agent has no tools" skillevals/behaviour.py 'Saying it has no tools is not a failure' 'Saying it is not a failure'

# --- 3. bundled example skills, read-only -------------------------------------
if (cd "$HERE" && python3 -m skillevals --skills-dir examples/skills lint >"$TMP/lint.log" 2>&1); then
  ok "lint of the seeded example skills ($(tail -1 "$TMP/lint.log"))"
else bad "lint of the seeded example skills"; grep -E '^FAIL' "$TMP/lint.log"; fi

echo
echo "selftest: $pass passed, $fail failed"
[[ $fail == 0 ]]
