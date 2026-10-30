#!/usr/bin/env bash
# selftest.sh - prove the harness does what the README says, offline (no model, no network).
#
# 1. Runs the unit tests (tests/).
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
mutant "api: another model's reply accepted"       skillevals/api.py 'if r.model != self.model:' 'if False:'
mutant "api: refusal read as a verdict"            skillevals/api.py 'if r.stop_reason == "refusal":' 'if False:'
mutant "api: truncated reply read as a verdict"    skillevals/api.py 'if r.stop_reason == "max_tokens":' 'if False:'
mutant "prices: worst case ignores input"          skillevals/prices.py 'return calls * (input_tokens * r_cw' 'return calls * (input_tokens * 0'
mutant "prices: preflight never refuses"           skillevals/prices.py 'if worst > max_usd:' 'if False:'
mutant "canary: API preflight skipped"             skillevals/__main__.py '_preflight(_canary_plan(cases' '(lambda *a: None)(_canary_plan(cases'
mutant "canary: grader calls left out of the plan" skillevals/__main__.py 'if isinstance(grader, behaviour.AnthropicGrader):' 'if False:'
mutant "canary: unknown agent kind runs claude -p" skillevals/__main__.py 'if kind == "claude":
        return behaviour.ClaudeAgent' 'if True:
        return behaviour.ClaudeAgent'
mutant "workspace: canary results written to the repo" skillevals/__main__.py 'p = behaviour.write_jsonl(rows, out)' 'p = behaviour.write_jsonl(rows, ROOT)'
mutant "api: any other stop reason read as a verdict" skillevals/api.py 'if r.stop_reason != "end_turn":' 'if False:'
mutant "canary: grader reads the uncut reply"      skillevals/behaviour.py 'r.text = r.text[:REPLY_CHARS]' 'pass'
mutant "report: API and CLI costs added together"  skillevals/report.py 'basis = r.get(basis_key) or "cli-list"' 'basis = "cli-list"'
mutant "prices: estimate counts characters, not bytes" skillevals/prices.py 'len(text.encode("utf-8"))' 'len(text)'
mutant "api: token usage dropped from a good reply" skillevals/api.py 'return Result(text, dt, cost, usage=usage)' 'return Result(text, dt, cost)'
mutant "api: billed error loses its token usage"    skillevals/api.py '"max_tokens", usage)' '"max_tokens")'
mutant "trigger: row drops the judge's token usage" skillevals/trigger.py 'usage=v.usage,' 'usage=None,'
mutant "canary: grader token usage not recorded"   skillevals/behaviour.py 'grader_usage=graw.usage)' 'grader_usage=None)'
mutant "api: refusal loses its token usage"         skillevals/api.py 'unspecified'"'"'}", usage)' 'unspecified'"'"'}")'
mutant "regrade: grader tokens counted on every row" skillevals/behaviour.py 'grader_usage=g.usage if i == 0 else None' 'grader_usage=g.usage'

mutant "regrade: billed grader call for a case with no graded reply" skillevals/behaviour.py 'if not old:  # every' 'if False:  # every'
mutant "regrade: file named after --agent, not the source run"       skillevals/__main__.py 'new_run_id(agents.pop())' 'new_run_id(agent.label)'
mutant "regrade: a file with two agents is accepted"                skillevals/__main__.py 'if len(agents) != 1:' 'if False:'
mutant "mine: OLLAMA_HOST check skipped"            skillevals/mine.py 'if u.scheme not in ("http", "https") or name not in LOOPBACK:' 'if False:'
mutant "scrub-check: a deny class removed (email)"   skillevals/mine.py 'for name, p in SECRET_RULES + DENY_RULES]' 'for name, p in SECRET_RULES + DENY_RULES[1:]]'
mutant "mine: tool output read as a typed prompt"    skillevals/mine.py 'return "\n".join(parts) if parts else None' 'return "\n".join(parts) if parts else str(content)'
mutant "mine: subagent turns read as the user's"     skillevals/mine.py 'if d.get("isSidechain"):' 'if False:'
mutant "mine: \"no worries\" read as a correction"   skillevals/mine.py 'r"^\s*(no|nope)\s*([,.!;:]|$)"' 'r"^\s*(no|nope)\b"'
mutant "accept: edited prompt not re-checked"        skillevals/mine.py 'found = scrub_check(text, names)' 'found = []'
mutant "accept: writes the public sets/"             skillevals/mine.py 'dest = Path(ws) / "sets" / f"{skill}.json"' 'dest = Path(public_sets) / f"{skill}.json"'
mutant "mine: workspace inside the repo allowed"     skillevals/__main__.py 'if mine.is_inside(ws, ROOT):' 'if False:'
# --- 3. bundled example skills, read-only -------------------------------------
if (cd "$HERE" && python3 -m skillevals --skills-dir examples/skills lint >"$TMP/lint.log" 2>&1); then
  ok "lint of the seeded example skills ($(tail -1 "$TMP/lint.log"))"
else bad "lint of the seeded example skills"; grep -E '^FAIL' "$TMP/lint.log"; fi

echo
echo "selftest: $pass passed, $fail failed"
[[ $fail == 0 ]]
