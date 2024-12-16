import asyncio
import queue

q = queue.Queue(maxsize=10)
stop = False

async def task1(q:queue.Queue):
    global stop
    print("task1 waiting for tha queue input")
    print(q.get(block=True))
    stop = True

async def task2():
    while not stop:
        print("---")
        await asyncio.sleep(1)

async def _main():
    t1= asyncio.create_task(task1(q=q))
    t2 = asyncio.create_task(task2())
    await asyncio.sleep(1)
    q.put(1)
    await asyncio.sleep(1)

    await t1
    await t2

if __name__ == "__main__":
    asyncio.run(_main())