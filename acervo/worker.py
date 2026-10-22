import sys

from .jobs import Runner

if __name__ == "__main__":
    result = Runner(int(sys.argv[1])).run()
    sys.exit(0 if result == "concluída" else 2)
