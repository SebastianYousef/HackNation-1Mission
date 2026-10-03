"""Job worker: `python -m atlas_api.worker`. Blocks on the Redis queue and runs
gap-search jobs. Run as many workers as you like; BRPOP hands each job to one."""
from __future__ import annotations

import asyncio
import json
import logging
import signal

from .cache import Store
from .config import Settings, get_settings
from .jobs import QUEUE, run_gap_search, save, status

log = logging.getLogger("atlas_api.worker")


async def handle(settings: Settings, store: Store, raw: bytes | str) -> None:
    """Run one queued job. A malformed payload marks its job failed (when the id is
    readable) and never stops the worker."""
    try:
        job = json.loads(raw)
    except ValueError:
        log.error("bad job payload %r", raw[:200])
        return
    job_id = job.get("job_id") if isinstance(job, dict) else None
    if (not isinstance(job_id, str) or job.get("kind") != "gap_search"
            or not all(isinstance(job.get(k), str) for k in ("disease_id", "label"))):
        log.error("bad job payload %r", raw[:200])
        if isinstance(job_id, str):
            await save(store, settings, status(job_id, "failed", error="invalid job payload"))
        return
    log.info("job %s %s", job_id, job["kind"])
    await run_gap_search(settings, store, job_id, job["disease_id"], job["label"])


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not settings.redis_url:
        raise SystemExit("worker needs REDIS_URL (without Redis the API runs jobs in-process)")
    store = Store.from_url(settings.redis_url, timeout=2)  # > the 1 s BRPOP block below
    assert store.redis is not None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("worker %s waiting on %s", settings.instance_id, QUEUE)
    while not stop.is_set():
        try:
            item = await store.redis.brpop([QUEUE], timeout=1)
        except Exception as exc:
            log.warning("redis error: %s", exc)
            await asyncio.sleep(2)
            continue
        if not item:
            continue
        try:
            await handle(settings, store, item[1])
        except Exception:
            log.exception("job crashed; worker keeps running")
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
