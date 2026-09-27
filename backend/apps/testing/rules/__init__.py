"""Business rules: scenarios of requests, and assertions checked after them.

``document`` reads and checks a rules file, ``expressions`` evaluates what is
written in it, and ``timeline`` does the date arithmetic. All three are pure:
running a scenario against an API is the runner's job.
"""
