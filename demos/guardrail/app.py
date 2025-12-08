# guardrail_server.py
from fastapi import FastAPI, Request
import re
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Simple patterns to detect potential prompt injection
INJECTION_PATTERNS = [
    r"ignore (all )?(previous|prior|above) instructions",
    r"forget (all )?(previous|prior|above)",
    r"disregard (all )?(previous|prior|above)",
    r"you are now",
    r"act as",
    r"pretend (to be|you are)",
]

@app.post("/scan")
async def scan(request: Request) -> dict:
    raw = await request.json()
    model = raw.get("model")
    messages = raw.get("messages", [])
    
    logger.info(f"Received scan request: model={model}, messages={len(messages)}")
    
    if not messages:
        logger.info("No messages to scan, allowing request")
        return {"allowed": True, "reason": "No messages to scan", "score": 0.0}
    
    # Check all messages for injection patterns
    for msg in messages:
        content = str(msg.get("content", "")).lower()
        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                logger.warning(f"Blocked request: matched pattern '{pattern}'")
                return {
                    "allowed": False,
                    "reason": f"Potential prompt injection detected: matches pattern '{pattern}'",
                    "score": 0.95
                }
    
    logger.info("Request passed all checks")
    return {"allowed": True, "reason": "No issues detected", "score": 0.0}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="localhost", port=8001)