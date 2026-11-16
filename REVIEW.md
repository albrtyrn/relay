# Relay: skeptical review (2026-11-16)

Scope: everything in the repo at commit `e6a6109` (M1 to M4 built, M5 not started). I read CLAUDE.md, PLAN.md, DESIGN.md, PROGRESS.md, all of `relay/`, `db/`, `scripts/`, and every test. **Nothing was fixed.** The only files added are this one and `scripts/break_guards.py`.

Two kinds of evidence are used below, and each claim says which one it rests on:

- **Ran it:** a mutation run (`scripts/break_guards.py`) or a probe (a throwaway test I ran against the real Postgres in the `tests` container; 27 probes, all passed). The probes are not in the repo; see section 7.
- **Read it:** reasoning from the code only. Treat these as hypotheses until a test exists.

Words used here:

- **Interleaving:** one particular order in which the steps of two or more processes happen.
- **Safety vs. liveness:** a safety problem is "something wrong happened" (a migration applied twice). A liveness problem is "something good never happens" (a job stuck forever).
- **Mutation:** deliberately removing one guard to see whether a test fails.

---

## 1. Summary

1. **I could not break any of the six invariants with an interleaving.** The four core mechanisms (compare-and-set, claim, lease token, marker) hold up, including in the four races PROGRESS.md lists as untested. I forced each of those and the code did what DESIGN.md says.
2. **One real finding is new:** at the retry limit, the worker decides `FAILED` from a marker read that does not wait for an attempt still in flight. I reproduced "Relay says `FAILED`, target has the column and the marker" (finding F1). It is the same family as the documented late-apply gap, but a wider window than the README describes.
3. **The rest are liveness and test-quality issues**, the largest being that nothing bounds how long a Relay transaction can hold a row lock (F3), and that short target problems become permanent `FAILED` (F4).
4. **Mutation results:** of the five guards you named, four are protected by a test that fails when the guard is removed. The fifth (idempotency reservation) does not exist yet. One extra guard, `SKIP LOCKED` in the reclaim sweep, has no test at all: removing it fails nothing.
5. **Biggest gap against PLAN.md:** mechanism 5 (idempotency keys) is entirely unbuilt, and no test makes a timeout fire, loses a connection during `COMMIT`, or races two reclaimers. The code handles all of those today; nothing in the suite would notice if it stopped.

---

## 2. Findings, ranked

Severity is my judgment of harm times likelihood for this project: **High** = wrong state with no warning under plausible conditions; **Medium** = wrong or stuck state that needs an unusual stall or outage; **Low** = cosmetic, test hygiene, or needs an administrator's mistake.

| # | Severity | Kind | Finding | Evidence |
| --- | --- | --- | --- | --- |
| F1 | Medium | Safety | Retry-limit path marks `FAILED` while an earlier attempt is still inside its target transaction | Ran it |
| F2 | Medium (accepted) | Safety | The documented late-apply gap (D23), stated as an exact sequence | Read it |
| F3 | Medium | Liveness | No timeout of any kind on `relay_db` connections: a frozen process holding a row lock blocks others without limit | Ran it |
| F4 | Medium | Liveness / design | Short target problems become permanent `FAILED` (one lock wait; or an outage longer than five leases) | Ran it (lock wait); read it (outage) |
| F5 | Low | Liveness | No network-level timeout on the target connection: a silently dead network can hang a worker indefinitely | Read it |
| F6 | Low | Test isolation | `test_lost_target_connection...` kills every `relay_migrator` session on the server, not only its own; crash tests can leak a worker process | Read it |
| F7 | Low | Policy | A column name ending in a newline passes the policy | Ran it |
| F8 | Low | Invariant 1 | "Only the helper writes `status`" is true today but nothing enforces or tests it | Read it |
| F9 | Low | Invariant 6 | One README sentence claims slightly more than its test shows | Read it + ran it |
| F10 | Info | API | `GET /migrations/{id}` reads the row and its events in two separate statements | Read it |

### F1. At the retry limit, `FAILED` is decided by a read that does not wait (Medium)

**Where:** `Worker._exhausted` in [relay/worker.py](relay/worker.py#L229), which calls `read_marker_hash` in [relay/target.py](relay/target.py#L161).

**Background.** In the normal execute path, the marker `INSERT` is the judge: if another attempt holds an uncommitted marker, the `INSERT` *waits* for it, so the new attempt cannot reach a verdict while the old one is undecided. The retry-limit path uses a plain `SELECT` instead. A `SELECT` never waits for another transaction's uncommitted insert; it simply does not see it.

**Sequence** (with `MAX_ATTEMPTS = 5`):

1. Attempts 1 to 4 are abandoned for any reason (target briefly unreachable, marker contention).
2. Worker A claims attempt 5, passes the lease re-check, begins its target transaction, inserts the marker, and stalls before `COMMIT` (a paused container, a slow disk at commit, a network stall). Its lease expires.
3. Worker B reclaims the job (`EXECUTING -> APPROVED`) and claims attempt 6. `6 > MAX_ATTEMPTS`, so B goes to `_exhausted`.
4. B reads `relay_applied`. A's marker is uncommitted, so B sees no row. B writes `EXECUTING -> FAILED`, "retry limit reached", with its own valid token.
5. A resumes and commits. Its final write to Relay matches 0 rows.

**Result:** Relay says `FAILED`; the target has the column and a marker with the approved hash.

**Reproduced** by a probe that holds the marker and the DDL in an open transaction as `relay_migrator` (standing in for A) and runs a worker whose claim is over the limit: outcome `failed`, Relay `FAILED (retry limit reached)`, and after the commit the target has marker and column. The contrast probe (same setup, under the limit) ends `abandoned` with nothing written, because the marker `INSERT` waits and then hits `lock_timeout`.

**Which invariant:** none of the six literally. Invariants 3 and 4 hold (B's write has a valid token; the marker is first and its hash matches). What breaks is what a reader takes `FAILED` to mean. The README already lists "a migration marked failed may still be applied later", but describes only "a worker that freezes just before its target transaction". This path needs no freeze *before* the transaction, and unlike the normal path it has no wait at all.

**How likely:** it needs attempt N to outlive its lease while inside the target transaction. D33 makes that require a stall of roughly 30 seconds or more beyond the timeouts (the timeouts bound each statement, not `COMMIT` and not a frozen process).

**Options to discuss (not done):** make the retry-limit check a waiting one (inside a target transaction, do the marker `INSERT`, then roll back: it waits exactly as the normal path does); or reword the README non-guarantee to cover this path; or wait for D23's fencing.

### F2. The documented late-apply gap, as an exact sequence (Medium, accepted in D23)

Listed so the review is complete; it is a known, accepted non-guarantee.

1. Worker A claims an approved job and passes the lease re-check (at least 30 s of lease left).
2. A freezes before `BEGIN` on the target for longer than its remaining lease.
3. Worker B reclaims and claims the job. B's DDL fails for a reason that later goes away (for example `lock_timeout` behind someone's long read). B marks `FAILED`.
4. A wakes. Nothing on the target tells it the job is over: no marker exists (B's rolled back with its DDL). A inserts the marker, runs the DDL, commits. Its Relay write matches 0 rows.

**Result:** `FAILED` in Relay, applied on the target. If step 3 had *succeeded* instead, A's marker insert would conflict and A would do nothing: the gap only hurts when the job ends `FAILED`. The lease token cannot help because the target never sees it; that is exactly what D23's fencing would add.

### F3. Nothing bounds a Relay transaction (Medium, liveness)

**Where:** `db.connect` in [relay/db.py](relay/db.py#L15) sets only a connect timeout. A probe read the session settings on a Relay connection: `lock_timeout = 0`, `statement_timeout = 0`, `idle_in_transaction_session_timeout = 0` (0 means "never").

Invariant 2 says never hold a transaction or row lock across slow work. The code obeys it for *planned* slow work. A process that freezes or loses its network *inside* a short transaction is unplanned slow work, and nothing ends it.

**Sequence A (reproduced):**

1. A reclaim sweep locks up to 10 expired rows (`SELECT ... FOR UPDATE SKIP LOCKED`), then its process freezes before `COMMIT`.
2. The owner of one of those rows finishes and runs its final `UPDATE`. It waits on the row lock.
3. It keeps waiting for as long as the frozen session stays connected. The probe saw it still in `Lock` wait 2 seconds later with nothing to time it out; the same would be true after 2 hours.

A blocked worker claims nothing else, so one frozen process can stop a second one.

**Sequence B (read it):** an API request freezes between the helper's `UPDATE` and `COMMIT` in approve. Every later approve or reject of that migration blocks in its `UPDATE`, so those HTTP requests hang rather than answer `409`.

**Sequence C (read it):** a worker freezes inside `claim_one` after the `SELECT ... FOR UPDATE`. Other claimers skip the row (correct), but the row cannot be claimed or reclaimed: it is `PENDING` with no lease, so the lease mechanism does not cover it.

No safety invariant is broken: when the frozen session finally dies, Postgres rolls it back and everything proceeds correctly. **Option to discuss:** set `idle_in_transaction_session_timeout` and `lock_timeout` on Relay connections, as the target transaction already does.

### F4. Short target problems become permanent `FAILED` (Medium, design)

Both follow from accepted decisions (D8, D9). They are listed because PLAN's concept list asks "which failures retry automatically and which need a human", and the honest answer today is harsher than it looks.

**(a) One lock wait fails the migration (ran it).** A session on the target holds any lock on `customers`, even a long read. The worker's `ALTER TABLE` waits, `lock_timeout` fires, and the job is `FAILED` on its first attempt with `ddl: 55P03: canceling statement due to lock timeout`. No retry. Target left clean (no marker, no column). This is D9 working as designed.

**(b) An outage longer than five leases fails every in-flight job without trying it (read it).**

1. The target is unreachable. A worker claims an approved job; `db.connect` fails; the pass ends `abandoned`. This repeats once per lease (60 s).
2. After five claims, `attempts` is 5. The sixth claim goes to `_exhausted`, which also cannot connect and abandons again, forever, while the outage lasts.
3. The target comes back. The next claim is still over the limit. `_exhausted` connects, finds no marker, and marks `FAILED`, "retry limit reached".

The DDL was never attempted once. A five-minute target outage therefore permanently fails every approved job that was claimed during it.

### F5. No network-level timeout on the target connection (Low, read it)

`statement_timeout` runs on the server. If the network path dies silently (no reset packet), the server finishes or cancels the statement but the worker never hears: it sits in a blocking read until the operating system's TCP keepalive gives up, which by default takes hours. The job is safe (the lease expires, the next worker's marker step settles it; if the lost statement was not `COMMIT`, the idle timeout rolls the transaction back). The worker is lost until then. With two workers, two such events stop all processing. Not reproduced.

### F6. Test isolation (Low, read it)

- `test_lost_target_connection_does_not_mark_failed` runs `pg_terminate_backend` on every session `WHERE usename = 'relay_migrator'`, on the whole server, and asserts exactly one was killed. The dev workers from `docker compose up` log in as the same role on the same server. If one of them is inside an execution when the test runs, the test kills the dev worker's session and fails its own assertion. Scoping the query with `AND datname = 'target_test'` would fix it.
- In both crash tests, if the worker subprocess does not exit within the timeout, `communicate` raises and the process is never killed. It keeps polling `relay_test` and can claim jobs in later tests.

### F7. Policy accepts a column name ending in a newline (Low, ran it)

`_COLUMN_NAME` in [relay/policy.py](relay/policy.py#L30) uses `$`, which in Python also matches just before a final newline. `ALTER TABLE customers ADD COLUMN "abc\n" text` (a quoted name ending in a newline) passes all nine rules. The statement is still a plain `ADD COLUMN`, so the harm is only an ugly column name. `re.fullmatch` or `\Z` closes it.

For the record, these also pass the policy and are then refused by the target as ordinary DDL errors (`FAILED`, nothing applied): a system column name such as `xmin`, and `numeric(5000, 2)`. That is the design working. `ALTER TABLE ONLY customers ...` is rejected by rule 9.

### F8. Invariant 1 rests on convention (Low, read it)

I searched `relay/` and `scripts/`: the only `UPDATE` of `status` is in `transition()`, and the only other write is the creation `INSERT`. So the invariant holds today. But no test would fail if someone added `UPDATE migrations SET status = ...` in another file. A two-line test that scans `relay/` for a second writer would turn the convention into a checked rule.

### F9. One README sentence is ahead of its test (Low)

"A lost target connection is not reported as a failure ... because the worker cannot know whether the target committed." The test behind it kills the session *before* the DDL, where the true outcome is always "rolled back". The case the sentence describes, a loss during `COMMIT` where the outcome really is unknown, has no test. My probes show the behavior is correct in both sub-cases (section 5), so the sentence is true; under invariant 6 it still needs its own test.

### F10. `GET /migrations/{id}` can show a row older than its events (Info)

The handler reads the row, then the events, as two autocommit statements. A transition that commits in between yields a response whose `status` is one step behind its last event. Harmless; one transaction (or one query) would make the response a single snapshot.

---

## 3. Invariant by invariant

| Invariant | Broken by an interleaving? | What I tried |
| --- | --- | --- |
| 1. Every status change goes through the helper | No | Searched for other writers (F8). Checked that `set_fields` cannot carry `status` (tested), that claim and reclaim both call the helper, and that the helper refuses to run outside a transaction (D28, tested). |
| 2. No transaction or lock across slow work | No, for planned work. See F3 for unplanned stalls | Draft path and execute path both verified from inside the slow step by existing tests. The lease re-check is a plain autocommit read. The target connection is opened outside any Relay transaction. |
| 3. Worker writes check token and status, atomically with the event | No | Every worker write (`R3`, `_succeed`, `_fail`, exhausted) passes its token. Forced: two reclaimers on one row; reclaimer against the owner's save in both orders and with a reclaim rollback; an executor overtaken mid-flight (new owner not finished). One winner, one event, every time. |
| 4. Marker first, same transaction; existing marker counts only with matching hash | No | Forced: connection cut during `COMMIT` in both directions; all three timeouts firing; marker contention through the full worker. F1 and F2 produce "`FAILED` but applied" without breaking the invariant's wording. |
| 5. Real Postgres, no mocks | Holds | No mock of the database anywhere. `FixedLLM`, `FlakyLLM`, `CountingLLM` are stand-ins for the LLM only, which is the intended fake. |
| 6. README claims only what a test shows | One soft spot (F9) | Compared each README bullet to its test. The thread-not-process and service-not-HTTP caveats are stated honestly. |

Why the two-reclaimer and reclaimer-versus-owner races are safe, in one paragraph: both sides must take the same row lock. `SKIP LOCKED` makes the sweep pass over a row someone else holds. If the row changed and committed just before the sweep locked it, Postgres re-checks the sweep's `WHERE` against the new row version and drops it. The owner's `UPDATE`, if it arrives second, waits and then re-checks its own `WHERE` (READ COMMITTED), finds the status or token changed, and matches 0 rows.

---

## 4. Does each concurrency and crash test force what it claims?

"Forced" means the test makes both sides reach the critical moment by construction (a hook, an open transaction, a check in `pg_stat_activity`). "Start-together" means threads are released at once and the test relies on them overlapping.

| Test | How the race or crash is made | Verdict |
| --- | --- | --- |
| `test_transitions::test_second_transaction_waits_then_matches_zero_rows` | A holds its update open; B is observed in `Lock` wait; then A commits | **Forced.** The cleanest race test in the suite. |
| `test_concurrent_approval::test_20_concurrent_approvals_exactly_one_wins` and `..._approve_and_reject_...` | Barrier only | **Start-together, not forced, but reliable in practice.** Nothing makes a loser reach the compare-and-set: one that reads the row after the winner commits gets its `409` from the plain read, and the test passes either way. I measured it: over 30 runs, all 570 losers lost at the compare-and-set, none at the read. The mutation was caught 3 runs out of 3. So it works by timing that happens to be very favorable, not by construction. A hook between the read and the helper call would make it forced. |
| `test_claim::test_two_claimers_one_row_second_gets_none`, `..._skips_the_locked_row_and_takes_the_next` | A's claim sits inside an outer transaction the test keeps open | **Forced.** Note what they prove: that B does not *wait*. With `SKIP LOCKED` removed, B would wait and then correctly get nothing, so these tests fail on B's `lock_timeout`. `SKIP LOCKED` here is a liveness guard; safety comes from the row lock plus the helper. |
| `test_claim::test_four_claimers_50_rows...`, both `test_worker_contention` tests | Barrier, 4 threads, 50 rows | **Start-together.** They assert an outcome that must hold in every interleaving (no row twice, exactly two events per job), so luck cannot make them pass wrongly; luck could only make them miss a rare bug. Removing the claim's whole lock clause was caught 3 of 3. The execution test never produces a marker conflict (each job is claimed once), so it is evidence for claiming, not for the marker. |
| `test_stale_worker::test_stale_drafter_write_matches_zero_rows`, `test_stale_executor_write_matches_zero_rows` | A paused by a hook; B runs to completion; A released | **Forced, but they do not test the token.** With the `lease_token` condition removed, both still pass, because the status has moved on and that alone stops A (confirmed by the mutation run). They prove "a stale worker cannot overwrite a finished job". |
| `test_stale_worker::test_stale_drafter_cannot_overwrite_a_new_owner_mid_draft` | Same, but B has claimed and not finished | **Forced, and the only worker-level test that needs the token.** There is no executing twin; my probe of that case passed, so the code is right and the test is simply missing. |
| `test_marker_contention` (both) | A paused inside its target transaction after the marker; B observed in `Lock` wait | **Forced.** They call `apply_on_target` directly, not through two workers. |
| `test_crash_generation`, `test_crash_after_target_commit` (both) | Real subprocess, `os._exit(137)` at a named line, or frozen then `SIGKILL` | **Forced, real process death.** Limits: the dead process's sockets close at once, so Postgres notices immediately; a machine or network loss where the connection lingers is not simulated. Leases are backdated, so no test waits out a real lease (the demo does). |
| `test_execute::test_lost_target_connection_does_not_mark_failed` | Hook kills the server session after the marker insert | **Forced, for a loss before the DDL only.** See F9 and F6. |
| `test_execute` lease tests (expired, too little, just enough, owner finishes late) | Hook edits `lease_expires_at` at an exact line | **Forced.** |
| `test_generation::test_claim_is_committed_before_llm_runs`, `test_execute::test_no_relay_transaction_is_open_during_target_work` | Observation from inside the slow step | **Forced.** Good direct tests of invariant 2. |
| `test_retry_limit` (all) | Backdated leases, a hook that raises once | **Deterministic; no race involved.** The crash is an in-process exception, not a process death. |

No test uses a sleep to create a race. The only sleeps are 10 ms polls that observe a state.

---

## 5. The four untested races you asked about

All four were forced with probes against real Postgres. In every case the code did what DESIGN.md says.

| Case | How it was forced | What happened |
| --- | --- | --- |
| **Two reclaimers, one expired row** | R1's sweep inside an open outer transaction; R2 sweeps at that moment | R2 returned 0 immediately, without waiting. After R1 committed: `PENDING`, exactly one reclaimer event. Massed version (8 reclaimers, 40 expired rows, barrier): 40 reclaimed in total, one event per row, no lease left. |
| **Reclaimer vs. the owner's save** | (a) owner's save transaction open, then sweep. (b) through the real worker: A paused at `draft.before_save`, reclaim left uncommitted, A released and observed in `Lock` wait, reclaim committed. (c) as (b) but the reclaim rolls back | (a) sweep skipped the row; owner's save stood. (b) A's save matched 0 rows, outcome `lost`, row `PENDING`. (c) A's save went through, `WAITING_FOR_APPROVAL`. One consistent outcome each time. |
| **Connection lost during the target `COMMIT`** | A small TCP relay between worker and Postgres that cuts the connection at the `COMMIT` message: once after the server processed it (answer lost), once before it arrived | Both: worker outcome `abandoned`, nothing written to Relay, lease kept. Answer lost: target had marker and column; after expiry the next worker found the marker and marked `SUCCEEDED` with no second DDL. Never arrived: target clean; the next worker applied it. This is DESIGN section 1 #8 behaving as written. |
| **`lock_timeout` / `statement_timeout` firing during DDL** | Another session holds a lock on `customers`; worker timeouts shortened to 300 ms | `lock_timeout`: `FAILED`, `ddl: 55P03: canceling statement due to lock timeout`, target clean, after 0.38 s. `statement_timeout` (with `lock_timeout` longer): `FAILED`, `ddl: 57014: canceling statement due to statement timeout`, target clean. Both match D9. See F4(a) for whether `FAILED` is the outcome you want. |

Two more from the same PROGRESS list, also forced:

- **`lock_timeout` on the marker step** (another transaction holds an uncommitted marker for the same id): `abandoned`, Relay untouched, and the retry succeeded after the other transaction rolled back. Contention is not treated as failure, as DESIGN section 1 #6 requires.
- **`idle_in_transaction_session_timeout` firing** (worker stalls inside its target transaction): the server ended the session, the worker's next statement failed on a dead connection, outcome `abandoned`, target clean. The dead-connection check correctly kept it from becoming `DDLFailed`.

### The rest of PROGRESS.md's "not yet tested" lists

| Item | Assessment |
| --- | --- |
| `run_forever`: polling, jitter, SIGTERM, reconnect, catch-all | **Highest-value gap left.** About 35 lines that run in every container and have no test. Read it: I found no bug. A marker that conflicts and then cannot be read raises `RuntimeError`, which is not a `psycopg.Error`; it escapes `run_once` and is caught by the catch-all, so the lease is abandoned, which is the right result reached by an untested path. |
| Late-apply gap | Not testable as a guarantee; it is a non-guarantee. F1 and F2 give the sequences. |
| Target unreachable at connect, and at the retry limit | Read it: both `abandoned`. Combined effect is F4(b). |
| Two workers colliding at the marker through the full worker path | Partly covered now: the F1 contrast probe ran a real worker against a held marker. |
| More than `batch_size` expired leases | The 40-row probe needed several sweeps and ended correct. A worker does one sweep of 10 per pass, so a large backlog drains over several passes. |
| 4 worker *processes* (not threads) | Still open. Only the crash tests use a process. Low risk: each thread has its own connections, and Postgres cannot tell the difference. |
| Concurrent approvals over real HTTP | Still open (D13, Phase 2 backlog). |
| API with the database down | Probe: approve with an unreachable database returns `500` with body `Internal Server Error`, no stack trace. A failure *mid-request* is still unexercised. |
| Other isolation levels | Not a gap: the level is pinned and the pin is tested. |
| `RELAY_HOOK=...:pause` | Stale entry: tested since M4. |
| "The worker placeholder container does nothing" (M1) | Stale entry: the worker is real since M3. |
| x64 build, `down -v` then `up`, `dbinit --reset` on dev, role password change, process exit on a bad lease, the demo script itself | Still open; all operational, none touches an invariant. |

---

## 6. Mutation results: which guards does a failing test protect?

Command (host, PowerShell or any shell):

```
python scripts/break_guards.py --full --repeat 3
```

It builds a temporary git worktree from the files on disk, removes one guard there, runs the tests that should catch it three times, then runs the whole suite (270 tests) against the mutant, restores the file, and deletes the worktree at the end. Baseline first: all 26 targeted tests passed with nothing removed. "Caught" below means an expected test failed in 3 runs out of 3.

**The five guards named in the request**

| Guard removed | Verdict | Tests that failed (whole suite) |
| --- | --- | --- |
| `AND status = ...` in the helper | **Caught** | 4: `test_wrong_from_status_returns_false_and_writes_no_event`, `test_second_transaction_waits_then_matches_zero_rows`, both `test_concurrent_approval` tests |
| `AND lease_token = ...` in the helper | **Caught**, thinly | 2: `test_wrong_lease_token_returns_false`, `test_stale_drafter_cannot_overwrite_a_new_owner_mid_draft`. Nothing on the execute path notices. |
| `SKIP LOCKED` in the claim | **Caught** | 2: `test_two_claimers_one_row_second_gets_none`, `test_second_claimer_skips_the_locked_row_and_takes_the_next` (both by lock timeout: a liveness failure, see section 4) |
| The `relay_applied` marker insert | **Caught**, broadly | 12, including both crash-after-commit tests, both marker-contention tests, the happy path, the stale executor, the 50-job execution test |
| Idempotency key reservation | **Not built** | `relay/idempotency.py` does not exist (M5). No guard, no test. The script has a placeholder that must be filled in at M5 and will stop with an error if it is forgotten. |

**Further guards I added**

| Guard removed | Verdict | Tests that failed (whole suite) |
| --- | --- | --- |
| `SKIP LOCKED` in the **reclaim** sweep | **No test; 0 of 270 failed** | Safety does not depend on it (a waiting sweep re-checks and finds nothing), but a sweep would then queue behind another worker's sweep or an owner's save. Unprotected. |
| The claim's whole `FOR UPDATE SKIP LOCKED` clause | Caught | 5: the 50-row claim test, both 50-job contention tests, both two-claimer tests |
| `AND lease_expires_at < now()` in reclaim | Caught | 8 |
| `if inserted is not None` (always run the DDL) | Caught | 6 |
| Marker hash comparison (any marker is success) | Caught by exactly 1 | `test_existing_marker_with_different_hash_fails` |
| `_rolled_back_cleanly` before `DDLFailed` | Caught by exactly 1 | `test_lost_target_connection_does_not_mark_failed` |
| Lease re-check before target work | Caught | 2 |
| Worker's approved-hash re-check | Caught by exactly 1 | `test_sql_not_matching_approved_hash_fails_without_touching_target` |
| API's stale-hash check | Caught | 2 |

Reading the table: the marker and the status check are protected many times over. Three guards hang on a single test each; that is enough, but worth knowing before anyone renames or deletes those tests.

Not mutated (no clean one-line removal exists): "the claim commits before work", "status change and audit event share a transaction", and the order "marker before DDL".

---

## 7. PLAN.md guarantees that no test demonstrates yet

**Not built (so nothing to test)**

1. Mechanism 5, idempotency keys, in full: key reserved in the same transaction as the change; two identical requests at once produce one mutation and identical responses; same key with a different body gives `422`; a retry after a lost response replays the saved answer.
2. Approve and reject "require `Idempotency-Key`" (endpoint table). Today the header is accepted and ignored, and a test pins that.
3. Test-plan row "Duplicate idempotency key".
4. Test-plan row "Concurrent approval ... each with a *different* idempotency key": the test exists, without keys.

**Built, behaves correctly in my probes, but no test in the suite**

5. Mechanism 4, step 1: `lock_timeout` and `statement_timeout` actually limiting the DDL. The suite checks the settings are in force, never that one fires. (PLAN's own Phase 2 test "a migration blocked by another transaction hits `lock_timeout`".)
6. A lost commit acknowledgement on the target is never recorded as a failure, and the next attempt settles it (DESIGN section 1 #8; implied by PLAN's "what if it fails, or runs twice?").
7. Reclaim under concurrency: two reclaimers on one row; a reclaimer against the owner's final write.
8. "`500` unexpected (no stack traces returned to clients)".
9. The stalled-transaction limit (`idle_in_transaction_session_timeout`) ending a stuck target transaction.

**Built, demonstrated only in a weaker form than PLAN states**

10. "Fire 20 approve **requests**": the test makes 20 service calls on 20 connections, not HTTP requests (D13, disclosed in the README).
11. "Create 50 jobs, **run 4 workers**": 4 threads in one process, not 4 worker processes (disclosed).
12. "Crash during generation: use a short test lease": the lease is backdated, never waited out (DESIGN section 1 #20). Only the demo script waits for a real lease, and the demo has no test.
13. "If the first worker was only paused and wakes up, every write it attempts includes `AND lease_token = ...`, matches 0 rows": demonstrated for a draft save. For `SUCCEEDED` and `FAILED` writes it is shown only where the status alone would also have stopped the write.
14. Invariant 1, "nothing else writes `status`": true by inspection, not by test (F8).
15. Invariant 2, "never hold a transaction across slow work": tested for the draft and execute steps; not for a process that stalls inside a short transaction (F3).

**Manual or environmental only**

16. Week 1 gate: restart every container and read the same record back (done once by hand, per PROGRESS).
17. "Both machines ... identical": the image has only been built on ARM64.
18. Week 4 gate: a fresh clone runs the tests and the demo from documented commands; CI. Phase 2.
19. `demo_crash.py` shows the recovery: run by hand; a test asserts the same sequence, not the script.

**PLAN statements the tests do demonstrate** (for contrast): exactly one of 20 approvals wins; approve and reject exclude each other; a stale hash cannot be approved or executed; terminal states reject every transition; a claim commits before the LLM or target work; a killed worker's job is reclaimed and finished; crash after the target commit leads to `SUCCEEDED` with no second DDL; a failed DDL leaves no schema change and no marker; a marker with a different hash is `FAILED`; a concurrent marker insert waits, then skips or proceeds; the target role's limits; the retry limit.

---

## 8. About the probes

The 27 probes are a throwaway pytest file that reuses the repo's fixtures. They are **not** in the repo and were run from outside it, so the suite is unchanged (still 270 tests). They are the natural starting point for real tests of items 5 to 9 above, plus F1. The TCP relay used for the `COMMIT` cut is about 60 lines of standard-library sockets, so it needs no new dependency.

Suggested order if you want to act on this review (each needs your OK first, since most touch transactions or the target):

1. Turn the probes for the four races and the timeouts into tests (no production code changes; they pass today).
2. Decide F1: waiting check at the retry limit, or widen the README non-guarantee.
3. Decide F3: timeouts on Relay connections.
4. Decide F4: whether `lock_timeout` on the DDL should retry (D9 was already marked "revisit in Phase 2"), and whether connect failures should count toward the limit.
5. Small ones: F6 (scope the kill, kill leaked processes), F7 (`fullmatch`), F8 (a single-writer test), F9 (covered by step 1).
