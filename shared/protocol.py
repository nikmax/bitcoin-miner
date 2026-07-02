"""Gemeinsame Konstanten für das Master/Worker-Protokoll."""

API_REGISTER = "/api/worker/register"
API_JOB = "/api/worker/job"
API_HEARTBEAT = "/api/worker/heartbeat"
API_FOUND = "/api/worker/found"
API_STATUS = "/api/status"

STATUS_IDLE = "idle"
STATUS_MINING = "mining"
STATUS_REGISTERED = "registered"
STATUS_OFFLINE = "offline"
STATUS_ERROR = "error"
STATUS_FOUND = "found"
