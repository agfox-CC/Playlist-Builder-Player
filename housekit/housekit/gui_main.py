"""Windowed entry for the packaged app (`HouseKit.exe`). Imports the package properly so
package-relative imports inside housekit work (running app.py directly as a script would not)."""
from housekit.app import main

if __name__ == "__main__":
    main()
