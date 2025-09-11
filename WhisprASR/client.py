import asyncio, json, websockets, sys

ASR_WS_URL = "ws://localhost:8080/ws"   

async def main(pcm_path: str):
    async with websockets.connect(ASR_WS_URL) as ws:
        await ws.send(json.dumps({"type":"start","sample_rate":16000,"language":"en"}))
        print("ACK:", await ws.recv())

        with open(pcm_path, "rb") as f:
            while chunk := f.read(frame_size):
                await ws.send(chunk)
                await asyncio.sleep(0.02)  

        await ws.send(json.dumps({"type":"stop"}))

        async for msg in ws:
            print("MSG:", msg)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python client.py output.pcm")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
