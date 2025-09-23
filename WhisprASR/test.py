import asyncio, json, aiohttp

async def main():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("http://localhost:8080/ws") as ws:
            await ws.send_json({"type":"start","sample_rate":16000,"language":"en","session_id":"e2e-test-1000"})
            await ws.send_json({"type":"stop"})
            async for msg in ws:
                print("WS:", msg.type, getattr(msg, "data", None))
asyncio.run(main())
