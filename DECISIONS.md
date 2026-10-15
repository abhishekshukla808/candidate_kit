# DECISIONS.md

1. **Indexes.** Startup creates a unique `employees.emp_code` index for identity and race-safe creation; `employees.department` and `(department, joined_on)` for exact department filters and active-headcount lookups; and `employees.joined_on` for all-department headcount. On attendance, unique `(emp_code, date)` enforces one record per employee-day and supports monthly and per-day joins; `(date DESC, emp_code ASC)` serves list sorting and month scans; `(emp_code, punch_in DESC)` serves latest-punch-out selection. They are created idempotently.

2. **Punch-in race.** Both requests derive the same attendance date and attempt `insert_one`. The unique `(emp_code, date)` index lets one insert commit; MongoDB rejects the other with `DuplicateKeyError`, which becomes 409. No preflight read is used as a concurrency guarantee.

3. **Ties.** `$setWindowFields` ranks only by `total_late_minutes`, so equal totals receive equal ranks. The output sort adds `emp_code` only after ranking. Filtering `rank <= limit` after the window means every tied employee at the cutoff is returned, even when the result contains more than `limit` rows.

4. **Headcount.** Department summary starts from employees joined by the month's last day and left-looks-up that employee's monthly logs. A preserved empty lookup still contributes one employee to the department group, so people with no logs count and future joiners do not. Departments with no eligible employees produce no result row.

5. **One thing I would change.** At 100 times the data, I would benchmark the lookup-heavy department pipelines and move monthly analytics to incrementally maintained summary collections if read latency justified the extra write path. I would first validate shard-key distribution and query targeting with production-like access patterns rather than shard by employee code by default; trend windows and cross-department summaries can otherwise become scatter-gather work.
