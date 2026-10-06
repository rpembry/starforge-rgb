# Lease bookkeeping extraction (issue #57)

`LeaseBook` owns active leases and retired subject generations. It handles expiry, retirement, cancellation, the finite subject-generation budget, and the severity/sequence winner lookup. `Coordinator` still owns the single lock, event admission, quiet policy, baseline, and output reconciliation. The coordinator calls the book only while holding that lock; the book performs no I/O and calls no sinks.

The extraction preserves the existing limits and ordering: expired tombstones are pruned before replay/rate records, and expired leases are retired after those records are pruned. A cancellation transfers the existing subject's reserved slot into retirement. A new subject-bearing generation receives `capacity` when the 2,048-slot active-or-retired budget is full; subjectless one-shots do not use that budget. `Coordinator._leases` and `_retired_subjects` remain compatibility aliases for existing host and test code during this incremental migration.

This is the first #57 slice and depends on the #38/#40 bounded-admission fixes and #39 active-terminal reserve. Admission policy and replay/rate tables remain in `Coordinator`; no second arbiter is introduced. Per-channel decisions (#51), asynchronous output actors (#54), and source binding (#53) require separate contract work. Fake and local-socket tests verify software behavior only.
