"""Stage both bookkeeping jobs before either worker starts a model request."""
import asyncio
import contextvars
import logging

queue_lock = asyncio.Lock()
_enqueue_tasks = set()


async def enqueue_event(event):
    import post_sentinel
    import kitchen_sentinel
    import kitchen
    generation = kitchen.generation()
    try:
        async with queue_lock:
            try:
                await post_sentinel.enqueue_event(event, wake=False)
            finally:
                await kitchen_sentinel.enqueue_event(event, expected_generation=generation, wake=False)
    finally:
        post_sentinel.wake()
        kitchen_sentinel.wake()


def observe_message_event(event):
    if event.get('type') not in ('msg_created', 'chatroom_msg_created'):
        return
    async def enqueue():
        try:
            await enqueue_event(event)
        except Exception:
            logging.getLogger(__name__).warning('Sentinel enqueue unavailable', exc_info=True)
    task = asyncio.create_task(enqueue(), context=contextvars.Context())
    _enqueue_tasks.add(task)
    task.add_done_callback(_enqueue_tasks.discard)


async def flush_enqueued():
    if _enqueue_tasks:
        await asyncio.gather(*list(_enqueue_tasks), return_exceptions=True)


async def stop():
    tasks = list(_enqueue_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
