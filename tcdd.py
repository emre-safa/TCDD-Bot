#!/usr/bin/env python3
"""TCDD train availability notifier — launcher.

Runs the bot. Implementation lives in the tcdd_bot/ package.

Setup
-----
  export TELEGRAM_BOT_TOKEN="<your bot token>"
  export TCDD_AUTH_TOKEN="<bearer token>"
  python tcdd.py     # or: python -m tcdd_bot
"""
from tcdd_bot.main import main


if __name__ == "__main__":
    main()
