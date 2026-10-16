"""Closed, value-free host evidence consumed by the existing schedule transport."""

WRITER_ACTION_MARKER = "recipient_writer_action"
WRITER_BINDING_MARKER = "recipient_writer_binding"
EXECUTION_FENCE_MARKER = "recipient_execution_fenced"
RUNTIME_OK_MARKER = "recipient_runtime_ok"
WRITER_ACTIONS = frozenset(
    {
        "pause",
        "resume",
        "inspect",
        "parked",
        "receipt_pause",
        "receipt_resume",
        "receipt_resume_retryable",
    }
)
