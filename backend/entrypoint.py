#!/usr/bin/python
import sys
import os

os.environ['PYTHONPATH'] = '/app/libs:' + os.environ.get('PYTHONPATH', '')

# Import and run uvicorn
from uvicorn.main import run
from uvicorn.config import Config

config = Config(
    app="main:app",
    host="0.0.0.0",
    port=8000,
    log_level="info"
)
server = run(config)
