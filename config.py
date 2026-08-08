import os
##########################
## JOBSTATS CONFIG FILE ##
##########################

# prometheus server address and port
PROM_SERVER = f"http://monitor1.{os.getenv('CLUSTER')}.ycrc.yale.edu:9090"
# prometheus server address, port, and retention period
PROM_RETENTION_DAYS = 14

# Set to True if GPU stats have jobid label as opposed to using nvidia_gpu_jobId
# This is available as of version 0.2.2 Sept 2025 in the repo
# https://github.com/plazonic/nvidia_gpu_prometheus_exporter/
GPU_EXPORTER_JOBID = True

# If using Slurm database then include the lines below with "enabled": False
# If using MariaDB/MySQL then set "enabled": True
# Set "mirror_to_admin_comment": True to additionally write the JS1 payload
# to the Slurm AdminComment field (sacctmgr). This preserves compatibility
# with sacct-based tools such as reportseff which read GPU/multi-node
# efficiency from AdminComment.
EXTERNAL_DB_CONFIG = {
    "enabled": False,  # set to True to use the external db for storing stats
    "host": "127.0.0.1",
    "port": 3307,
    "database": "jobstats",
    "user": "jobstats",
    "password": "password",
#     "config_file": "/path/to/jobstats-db.cnf",
#     "mirror_to_admin_comment": False,  # also write JS1 payload to AdminComment via sacctmgr
}

# Kafka producer for the jobstats -> Druid pipeline (see kafka_handler.py).
# One network-restricted broker, no auth, plain JSON. Every cluster writes the
# same topic; the "cluster" field on each record distinguishes rows. There is no
# enable flag: the producer is a dedicated admin tool that emits whenever run,
# gated by deployment (installed only where wanted -- e.g. not on Hopper).
KAFKA_CONFIG = {
    "bootstrap_servers": os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka.ycrc.yale.edu:9092"),
    "topic": os.environ.get("KAFKA_TOPIC", "slurm_jobstats"),
}

# Druid read-back: lets `jobstats <jobid>` reconstruct a report for jobs that
# have aged out of Prometheus, by fetching the stored JS1 payload back from the
# slurm_jobstats datasource (see druid_handler.py). No auth (network-restricted).
#
# Enabled per-cluster, derived from $CLUSTER the same way PROM_SERVER is above:
# on where the pipeline runs and Druid is reachable, off everywhere else. Hopper
# in particular is airgapped and has no Druid, so it must never default on.
# $DRUID_ENABLED overrides in either direction (e.g. to test before rollout).
DRUID_CLUSTERS = ("bouchet",)
DRUID_CONFIG = {
    "enabled": os.environ.get(
        "DRUID_ENABLED",
        "true" if os.getenv("CLUSTER") in DRUID_CLUSTERS else "false",
    ).lower() == "true",
    "url": os.environ.get("DRUID_URL", "http://druid.ycrc.yale.edu:8888/druid/v2/sql"),
    "datasource": os.environ.get("DRUID_DATASOURCE", "slurm_jobstats"),
}

# number of seconds between measurements
SAMPLING_PERIOD = 30

# threshold values for red versus black text and notes
GPU_UTIL_RED   = 15  # percentage
GPU_UTIL_BLACK = 25  # percentage
CPU_UTIL_RED   = 65  # percentage
CPU_UTIL_BLACK = 80  # percentage
TIME_EFFICIENCY_RED   = 10  # percentage
TIME_EFFICIENCY_BLACK = 60  # percentage
MIN_MEMORY_USAGE      = 70  # percentage
MIN_RUNTIME_SECONDS   = 10 * SAMPLING_PERIOD  # seconds

# translate cluster names in Slurm DB to informal names
CLUSTER_TRANS = {}  # if no translations then use an empty dictionary
CLUSTER_TRANS_INV = dict(zip(CLUSTER_TRANS.values(), CLUSTER_TRANS.keys()))
CLUSTER = os.getenv('CLUSTER')
# maximum number of characters to display in jobname
MAX_JOBNAME_LEN = 64


################################################################################
##                         C U S T O M    N O T E S                           ##
##                                                                            ##
##  Be sure to work from the examples. Pay attention to the different quote   ##
##  characters when f-strings are involved.                                   ##
################################################################################

NOTES = []

###############################
# B O L D   R E D   N O T E S #
###############################

# zero GPU utilization (single GPU jobs)
condition = 'self.js.gpus and (self.js.diff > c.MIN_RUNTIME_SECONDS) and num_unused_gpus > 0 ' \
            'and self.js.gpus == 1'
note = ("This job did not use the GPU. Please resolve this " \
        "before running additional jobs. Wasting resources " \
        "will cause your subsequent jobs to have a lower priority. " \
        "Is the code GPU-enabled? " \
        "Please consult the documentation for the software. For more info:",
        "https://docs.ycrc.yale.edu")
style = "bold-red"
NOTES.append((condition, note, style))

# zero GPU utilization (multi-GPU jobs)
condition = 'self.js.gpus and (self.js.diff > c.MIN_RUNTIME_SECONDS) and num_unused_gpus > 0 ' \
            'and self.js.gpus > 1'
note = ('f"This job did not use {num_unused_gpus} of the {self.js.gpus} allocated GPUs. "' \
        '"Please resolve this before running additional jobs. "' \
        'f"Wasting resources will cause your subsequent jobs to have a lower priority. {multi}"' \
        '"Please consult the documentation for the software. For more info:"',
        "https://docs.ycrc.yale.edu")
style = "bold-red"
NOTES.append((condition, note, style))

# zero CPU utilization (single node)
condition = '(self.js.diff > c.MIN_RUNTIME_SECONDS) and (num_unused_nodes > 0) ' \
            'and (self.js.nnodes == "1")'
note = ('"This job did not use the CPU. This suggests that something went wrong "' \
        '"at the very beginning of the job. For batch jobs, check your Slurm and "' \
        '"application scripts for errors and look for useful information in the "' \
        'f"file slurm-{self.js.jobid}.out if it exists."')
style = "bold-red"
NOTES.append((condition, note, style))

# zero CPU utilization (multiple nodes)
condition = '(self.js.diff > c.MIN_RUNTIME_SECONDS) and (num_unused_nodes > 0) ' \
            'and (int(self.js.nnodes) > 1)'
note = ('f"This job did not use {num_unused_nodes} of the {self.js.nnodes} allocated "' \
        'f"nodes. Please resolve this before running additional jobs. {multi_cpu}Please "' \
        '"consult the documentation for the software. For more info:"',
        "https://docs.ycrc.yale.edu")
style = "bold-red"
NOTES.append((condition, note, style))

# low GPU utilization (ondemand and salloc)
condition = '(not zero_gpu) and self.js.gpus and (self.js.gpu_utilization <= c.GPU_UTIL_RED) ' \
            'and interactive_job and (self.js.diff / SECONDS_PER_HOUR > 8)'
note = ('f"The overall GPU utilization of this job is only {round(self.js.gpu_utilization)}%. "' \
        '"This value is low compared to the cluster mean value of 50%. Please "' \
        '"do not create \'salloc\' or OnDemand sessions for more than 8 hours unless you "' \
        '"plan to work intensively during the entire period."')
style = "bold-red"
NOTES.append((condition, note, style))

# low GPU utilization (batch jobs)
condition = '(not zero_gpu) and self.js.gpus and (self.js.gpu_utilization <= c.GPU_UTIL_RED) ' \
            'and (not interactive_job)'
note = ('f"The overall GPU utilization of this job is only {round(self.js.gpu_utilization)}%. "' \
        '"This value is low compared to the cluster mean value of 50%. Please "' \
        '"investigate the reason for the low utilization. For more info:"',
        "https://docs.ycrc.yale.edu")
style = "bold-red"
NOTES.append((condition, note, style))

# low CPU utilization (red, more than one core)
condition = '(not zero_cpu) and (not self.js.gpus) and (self.js.cpu_efficiency < c.CPU_UTIL_RED) ' \
            'and (int(self.js.ncpus) > 1)'
note = ('f"The overall CPU utilization of this job is {ceff}%. This value "' \
        'f"is{somewhat}low compared to the target range of "' \
        'f"90% and above. Please investigate the reason for the low efficiency. "' \
        '"For instance, have you conducted a scaling analysis? For more info:"',
        "https://docs.ycrc.yale.edu")
style = "bold-red"
NOTES.append((condition, note, style))

# low CPU utilization (red, serial job)
condition = '(not zero_cpu) and (not self.js.gpus) and (self.js.cpu_efficiency < c.CPU_UTIL_RED) ' \
            'and (int(self.js.ncpus) == 1)'
note = ('f"The overall CPU utilization of this job is {ceff}%. This value "' \
        'f"is{somewhat}low compared to the target range of "' \
        'f"90% and above. Please investigate the reason for the low efficiency. "' \
        '"For more info:"',
        "https://docs.ycrc.yale.edu")
style = "bold-red"
NOTES.append((condition, note, style))

# out of memory
condition = 'self.js.state == "OUT_OF_MEMORY" and (not zero_cpu)'
note = ("This job failed because it needed more CPU memory than the amount that " \
        "was requested. If there are no other problems then the solution is to " \
        "resubmit the job while requesting more CPU memory by " \
        "modifying the --mem-per-cpu or --mem Slurm directive. For more info: ",
        "https://docs.ycrc.yale.edu")

style = "bold-red"
NOTES.append((condition, note, style))

# timeout
condition = '(self.js.state == "TIMEOUT") and (not zero_gpu) and (not zero_cpu)'
note = ("This job failed because it exceeded the time limit. If there are no " \
        "other problems then the solution is to increase the value of the " \
        "--time Slurm directive and resubmit the job. For more info:",
        "https://docs.ycrc.yale.edu")
style = "bold-red"
NOTES.append((condition, note, style))

# excessive run time limit (red)
condition = 'self.js.time_eff_violation and self.js.time_efficiency <= c.TIME_EFFICIENCY_RED ' \
            'and (not zero_gpu) and (not zero_cpu)'
note = ('f"This job only needed {self.js.time_efficiency}% of the requested time "' \
        'f"which was {self.human_seconds(SECONDS_PER_MINUTE * self.js.timelimitraw)}. "' \
        '"For future jobs, please request less time by modifying "' \
        '"the --time Slurm directive. This will "' \
        '"lower your queue times and allow the Slurm job scheduler to work more "' \
        '"effectively for all users. For more info:"',
        "https://docs.ycrc.yale.edu")
style = "bold-red"
NOTES.append((condition, note, style))

#########################
# P L A I N   N O T E S #
#########################

# always-displayed footer: link to the graphical job-efficiency report
condition = 'True'
note = ("To view a graphical report of this job's performance, see:",
        'f"https://ood-{self.js.cluster}.ycrc.yale.edu/pun/sys/ycrc_userportal/jobefficiency/{self.js.jobid}"',
        "Have a nice day!")
style = "normal"
NOTES.append((condition, note, style))
