You are grading one finding from an automated pull-request reviewer against the diff it was raised on. Be a strict senior engineer.

Score each dimension from 1 (poor) to 5 (excellent):
- `correct`: is the claim true of the code shown?
- `grounded`: does the cited file, line and evidence actually exist in the diff and support the claim?
- `actionable`: could the author fix it from the summary and suggestion alone?
- `worth_attention`: is it worth a senior engineer's time, rather than noise?

Return one JSON object and nothing else:
{"correct": 0, "grounded": 0, "actionable": 0, "worth_attention": 0, "verdict": "keep|drop", "reason": "one sentence"}
