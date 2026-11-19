"""Web tests run against a throwaway SQLite file, never the configured DATABASE_URL."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="medora-test-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_tmp, "test.db").replace("\\", "/")
