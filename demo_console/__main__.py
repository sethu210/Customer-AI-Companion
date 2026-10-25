r"""
Demo console web page.

    .\.venv\Scripts\python.exe -m demo_console [--port 8095]
"""

import argparse

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()  # same .env as the agent server: backend choice and Databricks settings
    parser = argparse.ArgumentParser(prog="demo_console", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8095)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run("demo_console.app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
