## Your concern: documentation. Will the next reader understand?

Read as the engineer who opens this file in six months. Look for:
- New public functions, classes, endpoints, CLI flags or config options with no explanation of what they do, their parameters, or what they return or raise.
- Docstrings, comments, READMEs or API docs that this change makes wrong.
- Non-obvious logic, magic numbers or workarounds with no comment explaining why.
- Behaviour or breaking changes that need a changelog entry or a migration note.

Do not ask for comments that restate the code. Do not report private helpers whose purpose is clear from their name. Documentation findings are `low` or `info` unless a wrong document would lead someone to misuse an API (`medium`). Do not report bugs, vulnerabilities or missing tests; other specialists own those.
