import asyncio
import os

import aiohttp


async def main():
 # Number of concurrent workers (coroutines) to run in parallel
 # Each worker will make requests independently
 concurrency = 10

 async def sending_coroutine(t: int, session: aiohttp.ClientSession) -> None:
  """
  Worker coroutine that makes multiple sequential API requests.

  Args:
   t: Worker ID number for tracking/debugging
   session: Shared aiohttp session for connection pooling

  Each worker makes 100 requests sequentially, sharing the same
  session across all requests to reuse TCP connections.
  """
  # Each worker makes 100 requests
  for i in range(100):
   # Use async context manager to ensure response is properly closed
   async with session.post(
    # Update for your endpoint, model, and input data
    # "a " * 1000 creates a ~1000-token string to test larger payloads
    f"https://api.voyageai.com/v1/embeddings",
    headers={"Authorization": f"Bearer {os.getenv('VOYAGE_API_KEY')}"},
    json={"model": "voyage-4-large", "input": ["a " * 1000]}
   ) as response:
    assert response.status == 200, (
     f"Response status code {response.status}: {await response.text()}"
    )
    # Log progress showing which worker and which iteration
    print(f"Processed request {i=} in thread {t=}")

  # Create a single session to be shared across all workers
  # This enables connection pooling and reduces overhead
  async with aiohttp.ClientSession() as session:
   # asyncio.gather runs all coroutines concurrently
   # The * operator unpacks the list comprehension into separate arguments
   # Creates 10 workers (t=0 through t=9), each sharing the same session
   await asyncio.gather(
    *[sending_coroutine(t, session) for t in range(concurrency)],
   )


if __name__ == "__main__":
 # Entry point: starts the async event loop and runs main()
 # asyncio.run() handles event loop creation, execution, and cleanup
 asyncio.run(main())