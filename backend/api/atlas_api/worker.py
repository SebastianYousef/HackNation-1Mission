"""Job worker: `python -m atlas_api.worker`. Blocks on the Redis queue and runs
gap-search jobs. Run as many workers as you like; BRPOP hands each job to one."""
from __future__ import annotations

import asyncio
import json
import logging
import signal

from .cache import Store
from .config import get_settings
from .jobs import QUEUE, run_gap_search

log = logging.getLogger("atlas_api.worker")


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not settings.redis_url:
        raise SystemExit("worker needs REDIS_URL (without Redis the API runs jobs in-process)")
    store = Store.from_url(settings.redis_url)
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
        job = json.loads(item[1])
        log.info("job %s %s", job["job_id"], job["kind"])
        if job["kind"] == "gap_search":
            await run_gap_search(settings, store, job["job_id"], job["disease_id"], job["label"])
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
