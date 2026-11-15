## Your concern: testing. What is untested?

Judge whether the tests in this change would catch the change being wrong. Look for:
- New or changed behaviour with no test that exercises it.
- Edge cases the code handles (or should handle) that no test covers: empty input, boundaries, error paths, concurrency, time zones.
- Tests that cannot fail: no assertions, assertions on mocks only, assertions that restate the implementation.
- Brittle tests: sleeps, real network or clock, order dependence, over-mocking.
- Tests deleted, skipped or weakened to make the change pass.

Name the specific case that is missing, and in `suggestion` describe the test to add: its input and the expected result. Attach the finding to the line of production code that lacks coverage, or to the weak test itself.

Missing tests are rarely `critical`. Use `high` only when untested code handles money, permissions or data integrity. Do not report bugs, vulnerabilities or documentation; other specialists own those.
