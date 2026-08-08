"""Read a stored JS1 payload back from Druid so `jobstats <jobid>` can rebuild a
report after the job has aged out of Prometheus (PROM_RETENTION_DAYS).

This is the Kafka-only replacement for the AdminComment / external-DB cache: the
slurm_jobstats datasource carries the opaque `js1` blob, and a point-lookup here
returns it for jobstats' existing JS1 decode path to reconstruct the report.
"""
try:
    import requests
except ImportError:
    requests = None
import sys

from config import DRUID_CONFIG


class DruidHandler:
    def __init__(self):
        self.enabled = DRUID_CONFIG.get("enabled", False)

    def get_jobstats(self, cluster, jobid):
        """Return the latest JS1 payload string for (cluster, jobid), or None."""
        if not self.enabled or requests is None:
            return None
        try:
            jid = int(str(jobid).strip())
        except (TypeError, ValueError):
            return None

        datasource = DRUID_CONFIG.get("datasource", "slurm_jobstats")
        query = (
            f'SELECT js1 FROM "{datasource}" '
            "WHERE cluster = ? AND jobid = ? AND js1 IS NOT NULL "
            "ORDER BY __time DESC LIMIT 1"
        )
        payload = {
            "query": query,
            "parameters": [
                {"type": "VARCHAR", "value": cluster},
                {"type": "BIGINT", "value": jid},
            ],
        }
        try:
            resp = requests.post(DRUID_CONFIG["url"], json=payload, timeout=10)
            resp.raise_for_status()
            rows = resp.json()
        except Exception as e:
            print(f"WARNING: Druid read-back failed for job {jobid}: {e}", file=sys.stderr)
            return None

        if rows and isinstance(rows, list):
            return rows[0].get("js1")
        return None
