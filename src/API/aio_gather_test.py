import asyncio
import random

async def func(i):
    await asyncio.sleep(random.random() * 10)
    return i

async def main():
    result = await asyncio.gather( *[ func(i) for i in range(20) ] )
    print(result)

if __name__ == "__main__":
    asyncio.run(main())