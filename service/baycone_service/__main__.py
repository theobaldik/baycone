import sys

from baycone_service.service import main

# Spawned job processes import this module too, they must not start a service of their own
if __name__ == "__main__":
    sys.exit(main())
