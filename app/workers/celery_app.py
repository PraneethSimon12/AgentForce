"""
The Celery application: configuration, and nothing else.

Every setting below is here because the default is wrong for durable work. Celery's
defaults are tuned for tasks you can afford to lose; ours are tuned for tasks whose whole
purpose is surviving the death of the process running them (D-023).

The three that matter most, and what each one prevents:

- **`task_acks_late = True`.** By default Celery acknowledges a task when it *starts*, so
  a worker killed mid-task takes the task with it — the broker already forgot. Late
  acknowledgement moves that to completion, which is what makes redelivery possible at
  all. This is CLAUDE.md §8's trap, and it is only half the fix.
- **`task_reject_on_worker_lost = True`.** The other half, and the one that gets missed.
  With late acks but this left at its default, a task whose worker is SIGKILLed is marked
  *failed* rather than requeued — you get the durability cost of late acks and none of
  the benefit. Together they make delivery at-least-once; the ledger makes the outcome
  effectively-once.
- **`visibility_timeout`.** Redis has no real acknowledgement channel, so "unacknowledged"
  is implemented as a timer: a task not finished within it is handed to another worker
  *while the first is still running*. Set below the time a tool takes and every long tool
  silently runs twice. It is derived from the tool timeout here rather than configured
  separately, because the failure mode of setting them inconsistently is invisible.

No result backend. A durable tool's result is a row in the ledger, because a result that
lives only in Redis is run state we would lose to a `FLUSHALL` — and D-003 says Redis
holds nothing we cannot lose.
"""

from __future__ import annotations

from celery import Celery

from app.settings import Settings, get_settings

TOOL_TASK = "agentforge.execute_tool"
"""
The durable-tool task's name on the wire.

A constant rather than an import, because the API process dispatches by *name*. That
keeps `send_task` from having to import the task module — and with it every tool handler
and its dependencies — into a process that is never going to run them.
"""

# The visibility timeout must exceed the longest a tool can legitimately take, or Redis
# redelivers work that is still in progress. Doubling leaves room for a worker that is
# briefly descheduled without making an actually-dead worker's task wait absurdly long.
_VISIBILITY_MULTIPLIER = 2


def build_celery_app(settings: Settings) -> Celery:
    """
    Build the Celery app from settings.

    Takes settings as an argument (D-013) so a test can build an app pointed at a
    different broker without touching the environment.
    """
    app = Celery("agentforge", broker=settings.redis_url)

    app.conf.update(
        # --- durability -------------------------------------------------------
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        # One at a time. With late acks a prefetched task is held hostage by whatever the
        # worker is currently doing, and a durable tool can be doing it for minutes.
        worker_prefetch_multiplier=1,
        broker_transport_options={
            "visibility_timeout": int(
                settings.durable_tool_timeout_seconds * _VISIBILITY_MULTIPLIER
            ),
        },
        # The broker is usually still starting when the worker is, under compose.
        broker_connection_retry_on_startup=True,
        # --- results ----------------------------------------------------------
        # Explicitly nothing. The ledger is the result store (D-023).
        result_backend=None,
        task_ignore_result=True,
        # --- serialisation ----------------------------------------------------
        # JSON only, and `accept_content` is the load-bearing half: pickle is Celery's
        # historical default and it makes anything that can write to the broker able to
        # execute arbitrary code in a worker. A tool's arguments are JSON already,
        # because they came from the model as JSON.
        task_serializer="json",
        accept_content=["json"],
        # --- time limits ------------------------------------------------------
        # Soft first so a tool gets a catchable exception and can clean up; hard shortly
        # after for one that ignores it. A tool killed by the hard limit leaves a PENDING
        # row, which is the ambiguous window, resolved by `effect_class` on redelivery.
        task_soft_time_limit=int(settings.durable_tool_timeout_seconds),
        task_time_limit=int(settings.durable_tool_timeout_seconds) + 30,
        # --- misc -------------------------------------------------------------
        timezone="UTC",
        enable_utc=True,
        # Imported explicitly rather than autodiscovered: the set of tasks a worker runs
        # is a decision, and autodiscovery makes it a consequence of the import graph.
        include=["app.workers.tasks"],
    )
    return app


# Module-level because `celery -A app.workers.celery_app worker` needs an app object to
# find, and a process entry point is the one place D-013's "no module-level settings
# singleton" cannot apply — there is nobody to inject into. `get_settings()` is still the
# single place the environment is read, and it is read exactly once here.
celery_app = build_celery_app(get_settings())
