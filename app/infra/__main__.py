"""允许通过 python -m app.infra 启动 Worker。"""

import asyncio
from app.infra.worker import main

asyncio.run(main())
