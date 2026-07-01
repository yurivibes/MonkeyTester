import asyncio
import json
import cv2
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay
from av import VideoFrame

# 0 is usually the integrated webcam, 1 or 2 is typically the external capture card
CAPTURE_CARD_INDEX = 0 

class VideoCaptureTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self):
        super().__init__()
        self.cap = cv2.VideoCapture(CAPTURE_CARD_INDEX)
        # Set resolution (optional, adjust to your capture card specs)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        
        # Read frame from capture card
        ret, frame = self.cap.read()
        if not ret:
            # If frame read fails, send a blank frame to keep stream alive
            import numpy as np
            frame = np.zeros((720, 1280, 3), dtype=np.uint8)

        # Convert OpenCV BGR to RGB for web browser compatibility
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        # Reformat into an AV VideoFrame object required by aiortc
        av_frame = VideoFrame.from_ndarray(rgb_frame, format="rgb24")
        av_frame.pts = pts
        av_frame.time_base = time_base
        return av_frame

relay = MediaRelay()

async def index(request):
    return web.FileResponse('index.html')

async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        if pc.connectionState == "failed" or pc.connectionState == "closed":
            await pc.close()
            pcs.discard(pc)

    # Instantiate our capture card video source and add it to WebRTC
    video_track = VideoCaptureTrack()
    pc.addTrack(relay.subscribe(video_track))

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.Response(
        content_type="application/json",
        text=json.dumps({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})
    )

pcs = set()

async def on_shutdown(app):
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()

app = web.Application()
app.on_shutdown.append(on_shutdown)
app.router.add_get('/', index)
app.router.add_post('/offer', offer)

if __name__ == "__main__":
    web.run_app(app, host='127.0.0.1', port=8078)

