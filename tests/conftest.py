import os

# The test database is selected before the app is imported; MONGO_URI comes from .env or the environment.
os.environ["MONGO_DB"] = "attendance_test"
