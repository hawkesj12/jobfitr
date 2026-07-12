"""jobfitr — the consumer web app on top of the job-radar engine.

A scheduled *wide* harvest caches the broad job universe into a jobs.json
snapshot; each user request applies their *narrow* lens to that cache using
job_radar's scoring primitives (reused unchanged). Zero external API calls per
request — user count is decoupled from job-API traffic.
"""

__version__ = "0.1.0"
