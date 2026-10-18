"""Business rules: scenarios of requests, and assertions checked after them.

``document`` reads and checks a rules file, ``expressions`` evaluates what is
written in it, and ``timeline`` does the date arithmetic; all three are pure.
``runner`` sends a file's scenarios to an API and returns what happened,
without touching the database — recording it is the service's job.
"""
