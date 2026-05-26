from __future__ import annotations

import uvicorn


if __name__ == "__main__":
    uvicorn.run("videorefiner_app.main:app", host="127.0.0.1", port=7860, reload=True)

