"""Entry point for the synthetic-only HTTP service."""

from agent_qa.server import Handler as Handler
from agent_qa.server import main


if __name__ == "__main__":
    main()
