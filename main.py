from dotenv import load_dotenv
import os
from pathlib import Path
load_dotenv(
	dotenv_path=Path(__file__).resolve().parent / ".env",
	override=True,
)
import asyncio

PROGRAM = os.getenv("PROGRAM")

if PROGRAM == "TAOHUA":
	print("\n\nRunning TAOHUA program")
	from main_taohua import main
else:
	print("\n\n✈️ Running program")
	from main_airport import main

if __name__ == "__main__":
    asyncio.run(main())        