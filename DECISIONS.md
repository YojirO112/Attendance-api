# DECISIONS.md

1. **Indexes.** `attendance_logs`: unique `(emp_code, date)` (one record per day, the PATCH key, per-employee month ranges, the `$lookup` join) and `(date desc, emp_code)` (list ordering, date ranges, the leaderboard's month scan). `employees`: unique `emp_code`, `(department, emp_code)` and `joined_on` (headcount). I rejected a `status`-leading index: five values, poor selectivity, extra cost on every write, and the date index already serves status filters.
2. **Punch-in race.** Both requests build the record and call `insert_one`. The unique index lets exactly one succeed; the other gets `DuplicateKeyError` and returns 409. There is no read before the write.
3. **Ties.** `$rank` gives 1, 2, 2, 4. I keep rows with `rank <= limit`, so everyone tied at the cutoff is returned and the list can be longer than `limit`. Tied rows are ordered by `emp_code`.
4. **Headcount.** The summary starts from `employees` (joined on or before month end), joins each person's logs and unwinds with `preserveNullAndEmptyArrays`. A person with no logs still produces one row, so they are counted once.
5. **At 100x.** Keep monthly per-employee totals updated on punch and correction, instead of re-reading logs, and use keyset pagination instead of `skip`.
