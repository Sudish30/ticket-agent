import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
from app import create_app

@pytest.fixture
def client():
    app = create_app(); app.testing = True
    return app.test_client()
