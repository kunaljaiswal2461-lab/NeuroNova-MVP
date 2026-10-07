import os

# Tests construct Settings directly. With no default for APP_ENV (SPEC-02),
# declare the test environment explicitly, once, for the whole suite.
os.environ.setdefault("APP_ENV", "dev")
os.environ.pop("RENDER", None)
