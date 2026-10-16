# DECISIONS.md

1. **Indexes.** Employees: unique `emp_code` (identity and the duplicate 409), `(department, emp_code)` for the filtered list, `(department, joined_on)` and `(joined_on)` for headcount. Attendance: unique `(emp_code, date)` (one record per day, and the race guard), `(date desc, emp_code)` for the default sort and month scans, `(status, date desc, emp_code)` for status filters, `(emp_code, punch_in desc)` for punch-out. I rejected a lone `status` index: five values, too unselective.

2. **Punch-in race.** Both requests compute the same date and call insert. The unique `(emp_code, date)` index lets exactly one succeed; the other gets a duplicate-key error and returns 409. No check-then-insert.

3. **Ties.** Ranking happens first, with `$rank`, and the limit filters on rank. Everyone tied at a rank within the limit is returned, so the list can be longer than `limit`.

4. **Headcount.** The pipeline starts from employees who joined by month end and joins their logs on, keeping employees with none, then counts distinct `emp_code`s.

5. **100x data.** Keep a daily rollup per department, updated on each write, so analytics read small summaries instead of scanning logs.
