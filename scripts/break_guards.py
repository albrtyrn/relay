"""Break one guard at a time and see whether a test notices ("mutation check").

    python scripts/break_guards.py                 run every mutation against its own tests
    python scripts/break_guards.py --full          also run the WHOLE suite against each mutation
    python scripts/break_guards.py --repeat 5      run each targeted check 5 times (is the catch reliable?)
    python scripts/break_guards.py --only status-check lease-token-check
    python scripts/break_guards.py --list

Run it on the host (not in a container), from anywhere inside the repo, with Docker running
and the Compose `postgres` service up. It needs only the Python standard library.

Words used here:
- Guard: one small piece of code that an invariant depends on, such as `AND status = ...` in
  the transition helper's UPDATE.
- Mutation: a deliberate one-line sabotage of a guard. A "mutant" is the code with one
  mutation applied.
- Caught: at least one of the tests that SHOULD notice the missing guard fails. A guard
  whose mutant passes every test is not protected by the suite, whatever the docs say.

How your working tree stays untouched:
  1. `git worktree add` makes a second checkout of HEAD in a temporary directory. (A
     worktree is an extra checkout that shares this repo's .git; it writes a little
     bookkeeping under .git/worktrees and nothing in your files.)
  2. Your current files (including uncommitted and untracked ones) are COPIED over it, so
     the mutants are made from what is on disk now, not from the last commit.
  3. Every mutation is applied to the copy, the tests run against the copy, and the file
     is restored in the copy.
  4. At the end the worktree is removed, even if the script fails or is interrupted.

How the tests run: `docker compose run tests`, as always, but with the temporary checkout
mounted at /mutant and used as the working directory, so `import relay` finds the mutant.
The tests use the `relay_test` and `target_test` databases, like a normal test run, so do
not run the suite elsewhere at the same time.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOUNT = "/mutant"
JUNIT_NAME = ".break_guards_junit.xml"

# Verdicts.
CAUGHT = "CAUGHT"            # an expected test failed: the guard is protected
NOT_CAUGHT = "NOT CAUGHT"    # every expected test still passed: the guard is NOT protected
HUNG = "HUNG"                # the run hit the timeout (noticed, but by hanging, not by failing)
NO_TEST = "NO TEST"          # no existing test is supposed to catch this
NOT_BUILT = "NOT BUILT"      # the guard's code does not exist yet


@dataclass(frozen=True)
class Mutation:
    name: str
    guard: str                       # what is removed, in plain words
    invariant: str                   # which CLAUDE.md invariant (or PLAN mechanism) it serves
    file: str                        # repo-relative path of the file to sabotage
    pattern: str                     # regular expression; must match EXACTLY once in the file
    replacement: str
    expected: tuple[str, ...]        # test ids (prefixes) that should fail on the mutant
    requested: bool = False          # one of the five guards named in the review request
    flags: int = 0


T = "tests/test_transitions.py::"
C = "tests/test_claim.py::"
E = "tests/test_execute.py::"

MUTATIONS = [
    # ---- the five guards named in the review request ----
    Mutation(
        name="status-check",
        guard="`AND status = <expected>` in the transition helper's UPDATE",
        invariant="1, 3",
        file="relay/transitions.py",
        pattern=r'conditions = \[sql\.SQL\("id = %\(id\)s"\), sql\.SQL\("status = %\(from_status\)s"\)\]',
        replacement='conditions = [sql.SQL("id = %(id)s")]  # MUTANT: status condition removed',
        expected=(
            T + "test_wrong_from_status_returns_false_and_writes_no_event",
            T + "test_second_transaction_waits_then_matches_zero_rows",
            "tests/test_concurrent_approval.py::test_20_concurrent_approvals_exactly_one_wins",
            "tests/test_concurrent_approval.py::test_concurrent_approve_and_reject_exactly_one_wins",
        ),
        requested=True,
    ),
    Mutation(
        name="lease-token-check",
        guard="`AND lease_token = <mine>` in the transition helper's UPDATE",
        invariant="3",
        file="relay/transitions.py",
        pattern=r'conditions\.append\(sql\.SQL\("lease_token = %\(lease_token\)s"\)\)',
        replacement="pass  # MUTANT: lease_token condition removed",
        expected=(
            T + "test_wrong_lease_token_returns_false",
            "tests/test_stale_worker.py::test_stale_drafter_cannot_overwrite_a_new_owner_mid_draft",
            "tests/test_stale_worker.py::test_stale_executor_cannot_overwrite_a_new_owner_mid_flight",
        ),
        requested=True,
    ),
    Mutation(
        name="skip-locked-claim",
        guard="`SKIP LOCKED` in the claim query (plain FOR UPDATE is left in)",
        invariant="3 (and liveness: claimers must not queue behind each other)",
        file="relay/claim.py",
        pattern=r"(LIMIT 1\s+FOR UPDATE) SKIP LOCKED",
        replacement=r"\1",
        expected=(
            C + "test_two_claimers_one_row_second_gets_none",
            C + "test_second_claimer_skips_the_locked_row_and_takes_the_next",
        ),
        requested=True,
    ),
    Mutation(
        name="marker-insert",
        guard="the INSERT into relay_applied (the DDL then runs with no marker)",
        invariant="4",
        file="relay/target.py",
        # The statement lives in `_insert_marker`, shared by apply_on_target and the
        # retry-limit probe (decision D39). The mutant claims "inserted" without inserting.
        pattern=r'inserted = target_conn\.execute\(\s+"""\s+INSERT INTO relay_applied.*?\)\.fetchone\(\)',
        replacement='inserted = {"migration_id": migration_id}  # MUTANT: marker insert removed',
        expected=(
            E + "test_happy_path_adds_column_writes_marker_marks_succeeded",
            "tests/test_crash_after_target_commit.py::",
            "tests/test_marker_contention.py::",
            "tests/test_stale_worker.py::test_stale_executor_write_matches_zero_rows",
        ),
        requested=True,
        flags=re.DOTALL,
    ),
    Mutation(
        name="idempotency-reservation",
        guard="the INSERT ... ON CONFLICT DO NOTHING that reserves the idempotency key",
        invariant="PLAN mechanism 5",
        file="relay/idempotency.py",
        # M5 is not built. When it is, replace this pattern with one that makes the code act
        # as if the key insert always returned a row (the request always "owns" the key).
        pattern=r"FILL IN AT M5: the reservation insert",
        replacement="",
        expected=("tests/test_idempotency.py::",),
        requested=True,
    ),

    # ---- more guards the invariants lean on ----
    Mutation(
        name="skip-locked-reclaim",
        guard="`SKIP LOCKED` in the reclaim sweep",
        invariant="liveness of reclaim (two reclaimers, or a reclaimer and the owner)",
        file="relay/claim.py",
        pattern=r"(LIMIT %\(batch_size\)s\s+FOR UPDATE) SKIP LOCKED",
        replacement=r"\1",
        # Both fail on the waiting sweep's lock timeout: this is a liveness guard (a sweep
        # must not queue behind another sweep or behind the owner's save), not a safety one.
        expected=(
            "tests/test_reclaim_races.py::test_second_reclaimer_skips_a_row_the_first_is_reclaiming",
            "tests/test_reclaim_races.py::test_reclaimer_skips_a_row_whose_owner_is_saving",
        ),
    ),
    Mutation(
        name="claim-row-lock",
        guard="the whole `FOR UPDATE SKIP LOCKED` clause of the claim query",
        invariant="3",
        file="relay/claim.py",
        pattern=r"(LIMIT 1)\s+FOR UPDATE SKIP LOCKED",
        replacement=r"\1",
        expected=(
            C + "test_two_claimers_one_row_second_gets_none",
            C + "test_four_claimers_50_rows_each_row_claimed_exactly_once",
            "tests/test_worker_contention.py::",
        ),
    ),
    Mutation(
        name="reclaim-expiry-check",
        guard="`AND lease_expires_at < now()` in the reclaim sweep",
        invariant="3",
        file="relay/claim.py",
        pattern=r"\s+AND lease_expires_at < now\(\)",
        replacement="",
        expected=("tests/test_reclaim.py::test_unexpired_lease_is_not_reclaimed",),
    ),
    Mutation(
        name="marker-result-ignored",
        guard="`if inserted:` in apply_on_target (the DDL runs whatever the marker insert returned)",
        invariant="4",
        file="relay/target.py",
        pattern=r"if inserted:",
        replacement="if True:  # MUTANT: run the DDL even when a marker already exists",
        expected=(
            E + "test_existing_marker_with_matching_hash_skips_ddl_and_succeeds",
            "tests/test_crash_after_target_commit.py::",
            "tests/test_marker_contention.py::test_second_inserter_waits_then_skips_when_first_commits",
        ),
    ),
    Mutation(
        name="marker-hash-compare",
        guard="the sql_hash comparison on an existing marker (any marker counts as success)",
        invariant="4",
        file="relay/target.py",
        # In `_judge_existing_marker`, shared by apply_on_target and the retry-limit probe.
        pattern=r'return ALREADY_APPLIED if existing\["sql_hash"\] == approved_hash else HASH_MISMATCH',
        replacement="return ALREADY_APPLIED  # MUTANT: hash not compared",
        expected=(
            E + "test_existing_marker_with_different_hash_fails",
            "tests/test_retry_limit.py::test_execution_at_limit_with_a_different_hash_marker_fails_and_says_so",
        ),
    ),
    Mutation(
        name="probe-rollback",
        guard="the ROLLBACK of the retry-limit probe's own marker insert (the probe then sees "
              "its own marker, reports 'applied', and commits a marker with no DDL)",
        invariant="4 (decision D39)",
        file="relay/target.py",
        pattern=r"raise psycopg\.Rollback",
        replacement="pass  # MUTANT: the probe keeps its marker",
        expected=(
            "tests/test_retry_limit.py::test_execution_at_limit_without_marker_fails",
            "tests/test_retry_limit.py::test_execution_at_limit_waits_for_an_in_flight_attempt_and_fails_when_it_rolls_back",
        ),
    ),
    Mutation(
        name="rollback-confirmed",
        guard="`_rolled_back_cleanly` before raising DDLFailed (unknown outcome becomes FAILED)",
        invariant="4 / DESIGN section 1 #8",
        file="relay/target.py",
        pattern=r" and _rolled_back_cleanly\(target_conn\)",
        replacement="",
        expected=(
            E + "test_lost_target_connection_does_not_mark_failed",
            "tests/test_timeouts_fire.py::test_idle_in_transaction_timeout_ends_a_stalled_target_transaction",
        ),
    ),
    Mutation(
        name="lease-recheck",
        guard="the lease re-check just before the target transaction (D33)",
        invariant="narrows the late-apply gap",
        file="relay/worker.py",
        pattern=r"if not self\._lease_covers_target_work\(claim\):",
        replacement="if False:  # MUTANT: lease re-check removed",
        expected=(
            E + "test_expired_lease_before_target_work_leaves_target_untouched",
            E + "test_lease_with_too_little_time_left_stops_before_target_work",
        ),
    ),
    Mutation(
        name="execute-hash-recheck",
        guard="the worker's check that the stored SQL still has the approved hash",
        invariant="PLAN mechanism 2",
        file="relay/worker.py",
        pattern=r"if sha256_hex\(claim\.proposed_sql\) != claim\.approved_sql_hash:",
        replacement="if False:  # MUTANT: approved-hash re-check removed",
        expected=(E + "test_sql_not_matching_approved_hash_fails_without_touching_target",),
    ),
    Mutation(
        name="approve-hash-binding",
        guard="the API's check that the reviewer's sql_hash is the stored proposal's hash",
        invariant="PLAN mechanism 2",
        file="relay/service.py",
        pattern=r'if row\["proposed_sql_hash"\] != sql_hash:',
        replacement="if False:  # MUTANT: stale-hash check removed",
        expected=(
            "tests/test_approve_reject.py::test_approve_stale_hash_409_and_state_unchanged",
            "tests/test_approve_reject.py::test_reject_stale_hash_409_and_state_unchanged",
        ),
    ),
]


@dataclass
class Result:
    mutation: Mutation
    verdict: str
    failed_expected: list[str] = field(default_factory=list)   # expected tests that failed (good)
    passed_expected: list[str] = field(default_factory=list)   # expected tests that still passed (bad)
    caught_runs: int = 0             # with --repeat: in how many runs something expected failed
    runs: int = 0
    full_suite_failures: list[str] | None = None               # with --full
    seconds: float = 0.0
    note: str = ""


# ---------- small helpers ----------


def run(command: list[str], *, cwd: Path = REPO, timeout: int | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, timeout=timeout, check=check, capture_output=True, text=True)


def say(text: str = "") -> None:
    print(text, flush=True)


def make_worktree(parent: Path) -> Path:
    """A second checkout of HEAD under `parent`, overlaid with the files on disk right now."""
    worktree = parent / "relay-mutants"
    run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"])

    # Bring the copy up to date with the working tree: tracked files as they are on disk,
    # plus untracked files that are not ignored. `-z` separates names with NUL bytes, so odd
    # file names survive.
    listed = run(["git", "ls-files", "-co", "--exclude-standard", "-z"]).stdout.split("\0")
    for name in filter(None, listed):
        source = REPO / name
        if source.is_file():
            destination = worktree / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    # Tracked files deleted in the working tree should be absent from the copy too.
    for name in filter(None, run(["git", "ls-files", "-d", "-z"]).stdout.split("\0")):
        (worktree / name).unlink(missing_ok=True)
    return worktree


def remove_worktree(worktree: Path) -> None:
    run(["git", "worktree", "remove", "--force", str(worktree)], check=False)
    shutil.rmtree(worktree, ignore_errors=True)
    run(["git", "worktree", "prune"], check=False)


def run_pytest(worktree: Path, test_ids: list[str], timeout: int, label: str) -> tuple[dict[str, str] | None, str]:
    """Run pytest in the tests container against the worktree.

    Returns ({test id: "passed" | "failed"}, output), or (None, output) if the run timed out
    or pytest could not even start (an import error counts as "could not start").
    """
    junit = worktree / JUNIT_NAME
    junit.unlink(missing_ok=True)
    container = f"relay-break-guards-{label}-{int(time.time())}"
    command = [
        "docker", "compose", "run", "--rm", "--name", container,
        "-v", f"{worktree}:{MOUNT}", "-w", MOUNT,
        # An expected id may be a prefix ending in "::" (meaning "every test in this file").
        # pytest wants the bare file path for that.
        "tests", "python", "-m", "pytest", *[test_id.removesuffix("::") for test_id in test_ids],
        "-q", "-p", "no:cacheprovider", f"--junitxml={MOUNT}/{JUNIT_NAME}",
    ]
    try:
        completed = run(command, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as expired:
        # The container is still running (probably stuck on a lock). Remove it.
        run(["docker", "rm", "-f", container], check=False)
        return None, f"TIMEOUT after {timeout}s\n{expired.stdout or ''}"
    output = completed.stdout + completed.stderr
    if not junit.exists():
        return None, output

    outcomes: dict[str, str] = {}
    for case in ElementTree.parse(junit).getroot().iter("testcase"):
        test_id = f"{case.get('classname', '').replace('.', '/')}.py::{case.get('name')}"
        broken = case.find("failure") is not None or case.find("error") is not None
        if case.find("skipped") is None:
            # A test can appear twice (a failure in the test, then an error in teardown).
            outcomes[test_id] = "failed" if broken or outcomes.get(test_id) == "failed" else "passed"
    junit.unlink(missing_ok=True)
    return outcomes, output


def matching(outcomes: dict[str, str], prefixes: tuple[str, ...], wanted: str) -> list[str]:
    return sorted(
        test_id for test_id, outcome in outcomes.items()
        if outcome == wanted and any(test_id.startswith(prefix) for prefix in prefixes)
    )


def check_imports_come_from_the_worktree(worktree: Path) -> None:
    """Fail early if the container would import the real repo instead of the mutant copy."""
    completed = run(
        [
            "docker", "compose", "run", "--rm", "-v", f"{worktree}:{MOUNT}", "-w", MOUNT,
            "tests", "python", "-c", "import relay.transitions as t; print(t.__file__)",
        ],
        timeout=120, check=False,
    )
    location = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
    if not location.startswith(MOUNT + "/"):
        raise SystemExit(
            "break_guards: the container did not import relay from the temporary checkout "
            f"(got {location!r}).\n{completed.stdout}{completed.stderr}"
        )


# ---------- one mutation ----------


def check_mutation(worktree: Path, mutation: Mutation, args) -> Result:
    result = Result(mutation=mutation, verdict=NO_TEST)
    target = worktree / mutation.file
    if not target.exists():
        result.verdict = NOT_BUILT
        result.note = f"{mutation.file} does not exist yet, so there is no guard to remove and no test of it"
        return result

    original = target.read_text(encoding="utf-8")
    mutated, count = re.subn(mutation.pattern, mutation.replacement, original, flags=mutation.flags)
    if count != 1:
        raise SystemExit(
            f"break_guards: mutation {mutation.name!r} matched {count} places in {mutation.file}, "
            "expected exactly 1. The code changed; update the pattern in scripts/break_guards.py."
        )

    started = time.monotonic()
    try:
        target.write_text(mutated, encoding="utf-8", newline="")
        if mutation.expected:
            for attempt in range(args.repeat):
                outcomes, output = run_pytest(worktree, list(mutation.expected), args.timeout, mutation.name)
                result.runs += 1
                if outcomes is None:
                    result.verdict = HUNG if output.startswith("TIMEOUT") else CAUGHT
                    result.note = "the run timed out" if result.verdict == HUNG else "pytest could not collect or start"
                    result.caught_runs += 1
                    continue
                failed = matching(outcomes, mutation.expected, "failed")
                passed = matching(outcomes, mutation.expected, "passed")
                if not failed and not passed:
                    # Nothing ran. Reporting that as "not caught" would be a lie.
                    raise SystemExit(f"break_guards: no expected test of {mutation.name!r} ran:\n{output[-3000:]}")
                if failed:
                    result.caught_runs += 1
                # Keep the weakest run: a test counts as catching only if it failed every time.
                result.failed_expected = failed if attempt == 0 else sorted(set(result.failed_expected) & set(failed))
                result.passed_expected = sorted(set(result.passed_expected) | set(passed))
            if result.verdict != HUNG:
                result.verdict = CAUGHT if result.caught_runs == result.runs else NOT_CAUGHT
                if 0 < result.caught_runs < result.runs:
                    result.note = f"caught in only {result.caught_runs} of {result.runs} runs: the catch depends on timing"
        else:
            result.note = "no existing test is aimed at this guard"

        if args.full:
            outcomes, output = run_pytest(worktree, [], args.full_timeout, mutation.name + "-full")
            if outcomes is None:
                result.full_suite_failures = ["(whole-suite run timed out or could not start)"]
            else:
                result.full_suite_failures = sorted(t for t, outcome in outcomes.items() if outcome == "failed")
    finally:
        target.write_text(original, encoding="utf-8", newline="")
    result.seconds = time.monotonic() - started
    return result


# ---------- reporting ----------


def report(results: list[Result], args) -> None:
    say()
    say("=" * 100)
    say(f"{'mutation':<26}{'verdict':<13}{'expected tests failed / still passed':<40}notes")
    say("-" * 100)
    for result in results:
        counts = f"{len(result.failed_expected)} failed / {len(result.passed_expected)} still passed" if result.runs else "-"
        say(f"{result.mutation.name:<26}{result.verdict:<13}{counts:<40}{result.note}")
    say("=" * 100)
    for result in results:
        say()
        say(f"[{result.verdict}] {result.mutation.name}  ({result.mutation.file}; invariant {result.mutation.invariant})")
        say(f"    removed: {result.mutation.guard}")
        for test_id in result.failed_expected:
            say(f"    failed (good):       {test_id}")
        for test_id in result.passed_expected:
            if test_id not in result.failed_expected:
                say(f"    STILL PASSED (bad):  {test_id}")
        if result.full_suite_failures is not None:
            say(f"    whole suite: {len(result.full_suite_failures)} test(s) failed")
            for test_id in result.full_suite_failures:
                say(f"        {test_id}")
        if result.note:
            say(f"    note: {result.note}")

    if args.json:
        payload = [
            {
                "name": r.mutation.name, "requested": r.mutation.requested, "file": r.mutation.file,
                "guard": r.mutation.guard, "invariant": r.mutation.invariant, "verdict": r.verdict,
                "failed_expected": r.failed_expected, "still_passed_expected": r.passed_expected,
                "caught_runs": r.caught_runs, "runs": r.runs,
                "full_suite_failures": r.full_suite_failures, "seconds": round(r.seconds, 1), "note": r.note,
            }
            for r in results
        ]
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        say(f"\nresults written to {args.json}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove one guard at a time and see whether a test fails.")
    parser.add_argument("--only", nargs="+", metavar="NAME", help="run only these mutations")
    parser.add_argument("--list", action="store_true", help="list the mutations and exit")
    parser.add_argument("--full", action="store_true", help="also run the whole suite against each mutant")
    parser.add_argument("--repeat", type=int, default=1, help="run each targeted check this many times")
    parser.add_argument("--timeout", type=int, default=240, help="seconds allowed for one targeted run")
    parser.add_argument("--full-timeout", type=int, default=900, help="seconds allowed for one whole-suite run")
    parser.add_argument("--json", metavar="PATH", help="also write the results as JSON to this file")
    args = parser.parse_args()

    if args.list:
        for mutation in MUTATIONS:
            say(f"{mutation.name:<26}{'*' if mutation.requested else ' '} {mutation.guard}")
        say("\n* = one of the five guards named in the review request")
        return 0

    chosen = [m for m in MUTATIONS if not args.only or m.name in args.only]
    unknown = set(args.only or ()) - {m.name for m in MUTATIONS}
    if unknown or not chosen:
        raise SystemExit(f"break_guards: unknown mutation name(s): {sorted(unknown)}. Try --list.")

    parent = Path(tempfile.mkdtemp(prefix="relay-break-guards-"))
    worktree = None
    results: list[Result] = []
    try:
        worktree = make_worktree(parent)
        say(f"temporary checkout: {worktree}")
        check_imports_come_from_the_worktree(worktree)

        # Baseline: with nothing broken, every expected test must pass. Otherwise a later
        # failure would prove nothing about the mutation.
        expected_everywhere = sorted({t for m in chosen for t in m.expected if (worktree / m.file).exists()})
        if expected_everywhere:
            say(f"baseline: {len(expected_everywhere)} test selection(s), no mutation ...")
            outcomes, output = run_pytest(worktree, expected_everywhere, args.timeout * 2, "baseline")
            missing = [] if outcomes is None else [
                t for t in expected_everywhere if not matching(outcomes, (t,), "passed")
            ]
            if outcomes is None or "failed" in outcomes.values() or missing:
                say(output[-4000:])
                if missing:
                    say(f"expected tests that did not run and pass: {missing}")
                raise SystemExit("break_guards: the baseline run did not pass; fix that before trusting any mutation result.")
            say(f"baseline: {len(outcomes)} tests passed")

        for mutation in chosen:
            say(f"mutation {mutation.name} ...")
            result = check_mutation(worktree, mutation, args)
            results.append(result)
            say(f"    {result.verdict}  ({result.seconds:.0f}s)")
    finally:
        if worktree is not None:
            remove_worktree(worktree)
        shutil.rmtree(parent, ignore_errors=True)

    report(results, args)
    # Exit code 1 if any existing guard is unprotected, so the script can gate a build later.
    return 1 if any(r.verdict in (NOT_CAUGHT, HUNG) for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
